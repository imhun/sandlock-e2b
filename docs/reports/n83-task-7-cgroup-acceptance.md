# Task 7 报告 —— N83 Phase 1：验收脚本 + 本地车道验收 + 文档 + 回退杆

工作树：`/Users/polus/project/ai/sandlock-e2b.wt/task7`（branch `n83/task7`，起点 = 集成分支
`n83-phase1-cgroup` 的 `95907a1`）。**没有 merge / rebase / push。**

> ## 本轮归档（acceptance-final 复跑，2026-10-06）
>
> 本文 §4/§5 的两份 JSON、以及全文引用的**每一个读数**，都是在 **`d600b7e`**（`n83/acceptance-final`，
> 即 `n83-phase1-cgroup` 并入 final-review 修复 `d8be58a` 之后的最终 commit）上，用同一支
> `deploy/scripts/acceptance/cgroup_acceptance.py`、同一条**本地 compose 多节点车道**重跑得到的原始
> stdout。车道：项目名 **`n83acc2`**、宿主端口 **3250**（用户 live 栈 `compose`:3100 全程未动）。
> 重跑命令见 §3.1；与 `164c987` 那一版（本文此前归档的读数）的逐项差异见 §0 的"读数移动"段。
>
> §2/§3 保留的仍是 Task 7 当时那条车道的配方说明（`n83acc` / 3200）——本轮沿用同一份配方，只换了
> 项目名与端口；附录 §F/§R 是当时各轮评审回修的历史记录，其中**引用读数的数字已同步到本轮归档 JSON**，
> 变更过程本身仍按其历史叙述。

## 0. 结论

* 验收脚本 **`deploy/scripts/acceptance/cgroup_acceptance.py`** 写好并跑通：**五条检查全绿**
  （`"ok": true`，退出码 0）。
* 验收在**本地 compose 多节点栈**（本轮复跑 `-p n83acc2`、宿主端口 **3250**；Task 7 那一趟是
  `-p n83acc`、3200 —— §2/§3 记的是那趟的配方）上跑；**k0s 集群一个 pod 都没碰**（这是
  `AGENTS.md` 的顺序：本地 lane 绿了才发线上）。
* 同一条车道、同一支脚本做了 **RED 档**（`E2B_SANDBOX_CGROUP=off`）：`measuredCpuPercent=400.52`、
  沙箱没有 cgroup、洪泛的 0.87 核记在 **hosting worker 自己容器的 cgroup** 上 —— N82 的症状在本地复现。
* **读数移动（`164c987` → `d600b7e`）**：GREEN 档整批时序读数换了一趟，量级与形状不变——
  ① `100.19→100.16`、静默 min `33.01→39.65 ms`、邻居 min `27.92→31.15 ms`、上限 `99.03→118.95 ms`；
  ② `usage_usec +3,189,901→+3,201,252`、`nr_throttled +31→+32`、`throttled_usec +9,301,843→+9,588,263`、
  `1.021→1.022` 核；③ 单跑 `10,165→9,794 op/s`（峰值 `0.840→0.840` 核）、四路 `12,071（3018×4）→
  11,945（2986×4）`、`0.994→0.994` 核、`nr_throttled +8→+12`。RED 档：① `399.92→400.52`、静默 min
  `31.37→27.45 ms`、邻居 min `29.98→26.84 ms`；③ 探针 `9118→9185 op/s`；§6 邻居账
  `worker-3 peak 0.872→worker-1 peak 0.869` 核（hosting worker 是哪台随调度变化，形状不变）。
  **未移动**：④ `peer_containers_count = 15`、`check4_mode = peer-container`、⑤ 三台全 `EACCES`，
  以及五条检查的 PASS/FAIL 判决（GREEN 全 PASS、RED 全 FAIL）。行文引用这些数的段落已逐处改到本轮
  归档的 JSON。
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

### 3.1 本轮（acceptance-final，`d600b7e`）重跑的命令

```bash
# 工作树：/Users/polus/project/ai/sandlock-e2b.wt/acc-final（branch n83/acceptance-final，HEAD = d600b7e）
# 依赖与构建产物
uv venv --python 3.14 tmp/venv
uv pip install --python tmp/venv/bin/python "e2b==2.46.0" httpx
mkdir -p wheels && cp -a /Users/polus/project/ai/sandlock-e2b/wheels/fork wheels/fork
cp deploy/compose/.env.example deploy/compose/.env   # 追加 AGENT_IMAGE=e2b-sandlock-agent:n83acc2
docker build -f deploy/docker/Dockerfile.agent -t e2b-sandlock-agent:n83acc2 .
docker compose -p n83acc2 -f deploy/compose/docker-compose.multinode.yml -f tmp/n83acc2-override.yml \
    build control-plane worker-1 worker-2 worker-3

# GREEN 档起栈 + 等三台委派（三台各一条 "cgroup lane ready"）
docker compose -p n83acc2 -f deploy/compose/docker-compose.multinode.yml -f tmp/n83acc2-override.yml up -d
docker logs n83acc2-worker-{1,2,3}-1 | grep 'cgroup lane ready'

# GREEN 验收（第一次；随后被 RED 打断，最终归档的是下面的第二次）
E2B_API_KEY=local-key tmp/venv/bin/python deploy/scripts/acceptance/cgroup_acceptance.py \
    --api-url http://127.0.0.1:3250 --api-key local-key --internal-key internal-key \
    --internal-url http://control-plane:3000 --nodes worker-1,worker-2,worker-3 \
    --worker-exec-template 'docker exec -i n83acc2-{node}-1 bash -lc' \
    --out tmp/n83acc2-acceptance-green.json

# RED 档（同一车道，开关翻回 off），同一条命令 → tmp/n83acc2-acceptance-red.json（exit 1，五条全 FAIL）
docker compose -p n83acc2 -f deploy/compose/docker-compose.multinode.yml -f tmp/n83acc2-override-off.yml up -d
tmp/venv/bin/python tmp/n83acc2-red-neighbour-cpu.py > tmp/n83acc2-red-neighbour-cpu.json   # RED 的"邻居付账"读数

# 复位 GREEN 档并复跑最终读数（= 本文 §4 归档的那一份）
docker compose -p n83acc2 -f deploy/compose/docker-compose.multinode.yml -f tmp/n83acc2-override.yml up -d
# 再跑一次同一条验收命令 → tmp/n83acc2-acceptance-green.json（exit 0，五条全 PASS）

# 拆栈
docker compose -p n83acc2 -f deploy/compose/docker-compose.multinode.yml -f tmp/n83acc2-override.yml down
```

一句实话：首次 `up` 之后立刻跑验收，第一个 create 会撞 `428: warm_required: base image not cached
on the target node` —— 控制面这一趟就是在做 base 镜像预热，**稍等片刻重跑同一条命令即绿**。这是本地
车道的预热，不是脚本问题，如实记一笔。

## 4. GREEN 原始读数（脚本 stdout，逐字）

```json
{
  "lane": {
    "api_url": "http://127.0.0.1:3250",
    "internal_url": "http://control-plane:3000",
    "worker_exec_template": "docker exec -i n83acc2-{node}-1 bash -lc",
    "nodes": [
      "worker-1",
      "worker-2",
      "worker-3"
    ],
    "cgroup_mount": "/pod-cgroup",
    "template": "base",
    "flood_seconds": 40.0,
    "worker_env": {
      "worker-1": {
        "pid1": "python",
        "observed": {
          "E2B_SANDBOX_CGROUP": {
            "value": "required",
            "source": "/proc/1/environ"
          },
          "E2B_CGROUP_MOUNT": {
            "value": "/pod-cgroup",
            "source": "/proc/1/environ"
          },
          "E2B_SANDBOX_NOTIFY_RATE_LIMIT": {
            "value": "0",
            "source": "/proc/1/environ"
          }
        }
      },
      "worker-2": {
        "pid1": "python",
        "observed": {
          "E2B_SANDBOX_CGROUP": {
            "value": "required",
            "source": "/proc/1/environ"
          },
          "E2B_CGROUP_MOUNT": {
            "value": "/pod-cgroup",
            "source": "/proc/1/environ"
          },
          "E2B_SANDBOX_NOTIFY_RATE_LIMIT": {
            "value": "0",
            "source": "/proc/1/environ"
          }
        }
      },
      "worker-3": {
        "pid1": "python",
        "observed": {
          "E2B_SANDBOX_CGROUP": {
            "value": "required",
            "source": "/proc/1/environ"
          },
          "E2B_CGROUP_MOUNT": {
            "value": "/pod-cgroup",
            "source": "/proc/1/environ"
          },
          "E2B_SANDBOX_NOTIFY_RATE_LIMIT": {
            "value": "0",
            "source": "/proc/1/environ"
          }
        }
      }
    }
  },
  "n82_baseline": {
    "ops_per_s": 18149,
    "cores_on_worker_pod": 1.02
  },
  "checks": {
    "1_quota_is_real": {
      "pass": true,
      "declared_cpu_percent": 100.0,
      "measured_cpu_percent": 100.15933739668868,
      "cpu_max_readback": "100000 100000",
      "sandbox_cgroup": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8/sbx_sbx_b6b96c66428cc73a",
      "spinner_node": "worker-1",
      "first_sandbox_rtt_quiet": {
        "samples_ms": [
          308.64,
          39.65,
          49.87,
          50.57,
          49.38
        ],
        "min_ms": 39.65,
        "median_ms": 49.87
      },
      "second_sandbox_rtt": {
        "samples_ms": [
          199.13,
          50.16,
          32.48,
          47.11,
          31.15
        ],
        "min_ms": 31.15,
        "median_ms": 47.11
      },
      "round_trip_criterion": "min-of-5 neighbour <= 3x the quiet min-of-5 (floor 50 ms) -- detects gross starvation (the N82 shape stalled 860 ms); a subtle slowdown is below its resolution and is caught by check 3's cgroup accounting instead",
      "round_trip_bound_ms": 118.95,
      "second_sandbox_node": "worker-1",
      "second_sandbox_same_node": true
    },
    "2_kernel_enforces": {
      "pass": true,
      "cgroup": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8/sbx_sbx_b6b96c66428cc73a",
      "cpu_max": "100000 100000",
      "window_s": 3.131,
      "usage_usec_delta": 3201252,
      "nr_throttled_delta": 32,
      "throttled_usec_delta": 9588263,
      "observed_cores": 1.022,
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
        "probe_output": "DONE op=openclose stalls=0 rounds=197 elapsed_s=40.2 ops_per_s=9794",
        "ops_per_s": 9794,
        "elapsed_s": 43.2,
        "sandbox_id": "sbx_bb2291cfcb4f3c0c",
        "node_id": "worker-1",
        "cgroup": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8/sbx_sbx_bb2291cfcb4f3c0c",
        "cgroup_samples": [
          {
            "at_s": 2.75,
            "path": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8/sbx_sbx_bb2291cfcb4f3c0c",
            "cpu_stat": {
              "usage_usec": 646981,
              "user_usec": 268285,
              "system_usec": 378695,
              "nice_usec": 0,
              "nr_periods": 8,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 4.87,
            "path": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8/sbx_sbx_bb2291cfcb4f3c0c",
            "cpu_stat": {
              "usage_usec": 2400327,
              "user_usec": 784850,
              "system_usec": 1615476,
              "nice_usec": 0,
              "nr_periods": 29,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 7.0,
            "path": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8/sbx_sbx_bb2291cfcb4f3c0c",
            "cpu_stat": {
              "usage_usec": 4135817,
              "user_usec": 1297382,
              "system_usec": 2838434,
              "nice_usec": 0,
              "nr_periods": 50,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 9.12,
            "path": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8/sbx_sbx_bb2291cfcb4f3c0c",
            "cpu_stat": {
              "usage_usec": 5894440,
              "user_usec": 1809704,
              "system_usec": 4084735,
              "nice_usec": 0,
              "nr_periods": 71,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 11.26,
            "path": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8/sbx_sbx_bb2291cfcb4f3c0c",
            "cpu_stat": {
              "usage_usec": 7663297,
              "user_usec": 2357142,
              "system_usec": 5306154,
              "nice_usec": 0,
              "nr_periods": 93,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 13.39,
            "path": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8/sbx_sbx_bb2291cfcb4f3c0c",
            "cpu_stat": {
              "usage_usec": 9431456,
              "user_usec": 2911070,
              "system_usec": 6520385,
              "nice_usec": 0,
              "nr_periods": 114,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 15.52,
            "path": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8/sbx_sbx_bb2291cfcb4f3c0c",
            "cpu_stat": {
              "usage_usec": 11170153,
              "user_usec": 3454802,
              "system_usec": 7715351,
              "nice_usec": 0,
              "nr_periods": 135,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 17.65,
            "path": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8/sbx_sbx_bb2291cfcb4f3c0c",
            "cpu_stat": {
              "usage_usec": 12942468,
              "user_usec": 3985891,
              "system_usec": 8956576,
              "nice_usec": 0,
              "nr_periods": 157,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 19.77,
            "path": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8/sbx_sbx_bb2291cfcb4f3c0c",
            "cpu_stat": {
              "usage_usec": 14687642,
              "user_usec": 4511664,
              "system_usec": 10175978,
              "nice_usec": 0,
              "nr_periods": 178,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 21.9,
            "path": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8/sbx_sbx_bb2291cfcb4f3c0c",
            "cpu_stat": {
              "usage_usec": 16436439,
              "user_usec": 5039773,
              "system_usec": 11396665,
              "nice_usec": 0,
              "nr_periods": 199,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 24.03,
            "path": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8/sbx_sbx_bb2291cfcb4f3c0c",
            "cpu_stat": {
              "usage_usec": 18181712,
              "user_usec": 5568803,
              "system_usec": 12612909,
              "nice_usec": 0,
              "nr_periods": 220,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 26.16,
            "path": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8/sbx_sbx_bb2291cfcb4f3c0c",
            "cpu_stat": {
              "usage_usec": 19941776,
              "user_usec": 6132268,
              "system_usec": 13809507,
              "nice_usec": 0,
              "nr_periods": 242,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 28.29,
            "path": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8/sbx_sbx_bb2291cfcb4f3c0c",
            "cpu_stat": {
              "usage_usec": 21731101,
              "user_usec": 6697775,
              "system_usec": 15033325,
              "nice_usec": 0,
              "nr_periods": 263,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 30.41,
            "path": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8/sbx_sbx_bb2291cfcb4f3c0c",
            "cpu_stat": {
              "usage_usec": 23471455,
              "user_usec": 7197850,
              "system_usec": 16273604,
              "nice_usec": 0,
              "nr_periods": 284,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 32.54,
            "path": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8/sbx_sbx_bb2291cfcb4f3c0c",
            "cpu_stat": {
              "usage_usec": 25237739,
              "user_usec": 7716931,
              "system_usec": 17520808,
              "nice_usec": 0,
              "nr_periods": 306,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 34.66,
            "path": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8/sbx_sbx_bb2291cfcb4f3c0c",
            "cpu_stat": {
              "usage_usec": 26988534,
              "user_usec": 8238939,
              "system_usec": 18749594,
              "nice_usec": 0,
              "nr_periods": 327,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 36.79,
            "path": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8/sbx_sbx_bb2291cfcb4f3c0c",
            "cpu_stat": {
              "usage_usec": 28733577,
              "user_usec": 8740496,
              "system_usec": 19993080,
              "nice_usec": 0,
              "nr_periods": 348,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 38.91,
            "path": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8/sbx_sbx_bb2291cfcb4f3c0c",
            "cpu_stat": {
              "usage_usec": 30498902,
              "user_usec": 9280732,
              "system_usec": 21218170,
              "nice_usec": 0,
              "nr_periods": 369,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 41.04,
            "path": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8/sbx_sbx_bb2291cfcb4f3c0c",
            "cpu_stat": {
              "usage_usec": 32256571,
              "user_usec": 9798023,
              "system_usec": 22458548,
              "nice_usec": 0,
              "nr_periods": 391,
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
          0.815,
          0.83,
          0.827,
          0.83,
          0.816,
          0.832,
          0.823,
          0.821,
          0.819,
          0.826,
          0.84,
          0.821,
          0.829,
          0.826,
          0.819,
          0.833,
          0.825
        ],
        "peak_cores": 0.84,
        "nr_throttled_delta": 0
      },
      "flood_under_own_spinners": {
        "label": "4 concurrent copies of the probe's openclose program, one sandbox",
        "harness": "probe_n82_traced_syscall_costs.INNER imported verbatim, run in a sandbox we own",
        "sandbox_id": "sbx_eceddeda0ebc57a4",
        "node_id": "worker-1",
        "cgroup": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8/sbx_sbx_eceddeda0ebc57a4",
        "ops_per_s": 11945,
        "per_client_ops_per_s": [
          2986,
          2986,
          2987,
          2986
        ],
        "output": "DONE op=openclose stalls=0 rounds=30 elapsed_s=20.1 ops_per_s=2986\nDONE op=openclose stalls=0 rounds=30 elapsed_s=20.1 ops_per_s=2986\nDONE op=openclose stalls=0 rounds=30 elapsed_s=20.1 ops_per_s=2987\nDONE op=openclose stalls=0 rounds=30 elapsed_s=20.1 ops_per_s=2986",
        "cgroup_samples": [
          {
            "at_s": 2.01,
            "path": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8/sbx_sbx_eceddeda0ebc57a4",
            "cpu_stat": {
              "usage_usec": 2108489,
              "user_usec": 916689,
              "system_usec": 1191800,
              "nice_usec": 0,
              "nr_periods": 24,
              "nr_throttled": 1,
              "throttled_usec": 17638,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 4.13,
            "path": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8/sbx_sbx_eceddeda0ebc57a4",
            "cpu_stat": {
              "usage_usec": 4214754,
              "user_usec": 1758831,
              "system_usec": 2455922,
              "nice_usec": 0,
              "nr_periods": 45,
              "nr_throttled": 5,
              "throttled_usec": 22349,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 6.28,
            "path": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8/sbx_sbx_eceddeda0ebc57a4",
            "cpu_stat": {
              "usage_usec": 6316067,
              "user_usec": 2626926,
              "system_usec": 3689140,
              "nice_usec": 0,
              "nr_periods": 66,
              "nr_throttled": 5,
              "throttled_usec": 22349,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 8.42,
            "path": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8/sbx_sbx_eceddeda0ebc57a4",
            "cpu_stat": {
              "usage_usec": 8414729,
              "user_usec": 3480345,
              "system_usec": 4934383,
              "nice_usec": 0,
              "nr_periods": 88,
              "nr_throttled": 7,
              "throttled_usec": 23310,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 10.55,
            "path": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8/sbx_sbx_eceddeda0ebc57a4",
            "cpu_stat": {
              "usage_usec": 10507357,
              "user_usec": 4330700,
              "system_usec": 6176657,
              "nice_usec": 0,
              "nr_periods": 109,
              "nr_throttled": 7,
              "throttled_usec": 23310,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 12.68,
            "path": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8/sbx_sbx_eceddeda0ebc57a4",
            "cpu_stat": {
              "usage_usec": 12615158,
              "user_usec": 5203223,
              "system_usec": 7411934,
              "nice_usec": 0,
              "nr_periods": 130,
              "nr_throttled": 10,
              "throttled_usec": 24490,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 14.82,
            "path": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8/sbx_sbx_eceddeda0ebc57a4",
            "cpu_stat": {
              "usage_usec": 14703467,
              "user_usec": 6062019,
              "system_usec": 8641448,
              "nice_usec": 0,
              "nr_periods": 152,
              "nr_throttled": 13,
              "throttled_usec": 26252,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 16.94,
            "path": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8/sbx_sbx_eceddeda0ebc57a4",
            "cpu_stat": {
              "usage_usec": 16808822,
              "user_usec": 6946418,
              "system_usec": 9862403,
              "nice_usec": 0,
              "nr_periods": 173,
              "nr_throttled": 13,
              "throttled_usec": 26252,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 19.08,
            "path": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8/sbx_sbx_eceddeda0ebc57a4",
            "cpu_stat": {
              "usage_usec": 18892887,
              "user_usec": 7757050,
              "system_usec": 11135837,
              "nice_usec": 0,
              "nr_periods": 194,
              "nr_throttled": 13,
              "throttled_usec": 26252,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 21.21,
            "path": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8/sbx_sbx_eceddeda0ebc57a4",
            "cpu_stat": {
              "usage_usec": 19876890,
              "user_usec": 8176646,
              "system_usec": 11700244,
              "nice_usec": 0,
              "nr_periods": 208,
              "nr_throttled": 13,
              "throttled_usec": 26252,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          }
        ],
        "interval_cores": [
          0.994,
          0.977,
          0.981,
          0.982,
          0.99,
          0.976,
          0.993,
          0.974,
          0.462
        ],
        "peak_cores": 0.994,
        "nr_throttled_delta": 12
      }
    },
    "4_narrowing_view_shape": {
      "pass": true,
      "criterion": "own delegated cgroup writable (cgroup.procs/subtree_control, NOT cpu.max); every visible peer container's cpu.max still EACCES; every non-delegated peer's cpu.max/cgroup.procs/mkdir EACCES",
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
          "hostname": "62bd58126036",
          "own": {
            "path": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8",
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
          "peer_containers": [
            {
              "path": "/pod-cgroup/docker/0315ea32389bc1f5ac4ae52379ea186022dbe241656afab580a2c5655ed1cf28",
              "owner": {
                "uid": 65534,
                "gid": 65534,
                "mode": "0o755"
              },
              "delegated_to_our_uid": true,
              "cpu_max": "EACCES",
              "cgroup_procs": "WRITABLE",
              "subtree_control": "WRITABLE",
              "mkdir": "WRITABLE"
            },
            {
              "path": "/pod-cgroup/docker/0e3d3ae4e6e52750f82c78a069e4306e086d938d0f6810d5747ee48d3ef33d91",
              "owner": {
                "uid": 65534,
                "gid": 65534,
                "mode": "0o755"
              },
              "delegated_to_our_uid": true,
              "cpu_max": "EACCES",
              "cgroup_procs": "WRITABLE",
              "subtree_control": "WRITABLE",
              "mkdir": "WRITABLE"
            },
            {
              "path": "/pod-cgroup/docker/2668a85ac283c423b406054f3a64f5588a822bcca716dec3517bccf13be9927e",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/31bf2ab1609c4510882b920115f9e360eec53681d25962e535527b2a93f71cd6",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/4266be855b7cb46812d6df1da5bc591e9c3f5b08c87e1832939b48051b857d9d",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/50f39f34350fbec9261df88c4ef276ba6f71ca491d5dda5b6cb29965dc31f935",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/55da732083d6c8b949367222b98e580dbef63d654d9e062f7af1229bfed6bef0",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/652abcef44b8e6e492179ac636d2233429d1582879110d668d700cee458336c0",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/69914a17e95613bd79573d13f82dfb2f841c400e1620186dc7e725740044b02d",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/9f6ba4a0c1a9b29c6555120bac22fbbe2938cb12e13581aa6dff07b89ac8410b",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/b8e70c8de35d6097a8290b29256f0ce2836dc18f984a6e05106dfdbc06457d08",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/buildkit",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/buildx",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/d891595d5af4caba9f1756870793044d04dd6aee97b38716eb275471d234bb56",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/df12053355f0d6fda015353119a675cb7044819ab010c93b551293d1e28d3398",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            }
          ],
          "mount_root": {
            "path": "/pod-cgroup",
            "owner": {
              "uid": 0,
              "gid": 0,
              "mode": "0o755"
            },
            "has_cpu_max_and_procs": true,
            "cpu_max": "EACCES",
            "cgroup_procs": "EACCES",
            "mkdir": "EACCES"
          },
          "own_subtree": {
            "own": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8",
            "children": [
              "sbx_sbx_eceddeda0ebc57a4",
              "worker"
            ],
            "proc_self_cgroup": "0::/worker"
          },
          "own_delegated": true,
          "peer_containers_count": 15,
          "foreign_peers": [
            "/pod-cgroup/docker/2668a85ac283c423b406054f3a64f5588a822bcca716dec3517bccf13be9927e",
            "/pod-cgroup/docker/31bf2ab1609c4510882b920115f9e360eec53681d25962e535527b2a93f71cd6",
            "/pod-cgroup/docker/4266be855b7cb46812d6df1da5bc591e9c3f5b08c87e1832939b48051b857d9d",
            "/pod-cgroup/docker/50f39f34350fbec9261df88c4ef276ba6f71ca491d5dda5b6cb29965dc31f935",
            "/pod-cgroup/docker/55da732083d6c8b949367222b98e580dbef63d654d9e062f7af1229bfed6bef0",
            "/pod-cgroup/docker/652abcef44b8e6e492179ac636d2233429d1582879110d668d700cee458336c0",
            "/pod-cgroup/docker/69914a17e95613bd79573d13f82dfb2f841c400e1620186dc7e725740044b02d",
            "/pod-cgroup/docker/9f6ba4a0c1a9b29c6555120bac22fbbe2938cb12e13581aa6dff07b89ac8410b",
            "/pod-cgroup/docker/b8e70c8de35d6097a8290b29256f0ce2836dc18f984a6e05106dfdbc06457d08",
            "/pod-cgroup/docker/buildkit",
            "/pod-cgroup/docker/buildx",
            "/pod-cgroup/docker/d891595d5af4caba9f1756870793044d04dd6aee97b38716eb275471d234bb56",
            "/pod-cgroup/docker/df12053355f0d6fda015353119a675cb7044819ab010c93b551293d1e28d3398"
          ],
          "foreign_peers_closed": true,
          "every_peer_cpu_max_closed": true,
          "delegated_peers": [
            {
              "path": "/pod-cgroup/docker/0315ea32389bc1f5ac4ae52379ea186022dbe241656afab580a2c5655ed1cf28",
              "cgroup_procs": "WRITABLE",
              "subtree_control": "WRITABLE",
              "mkdir": "WRITABLE",
              "cpu_max": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/0e3d3ae4e6e52750f82c78a069e4306e086d938d0f6810d5747ee48d3ef33d91",
              "cgroup_procs": "WRITABLE",
              "subtree_control": "WRITABLE",
              "mkdir": "WRITABLE",
              "cpu_max": "EACCES"
            }
          ],
          "peer_visible": true,
          "mount_looks_narrowed": false,
          "check4_mode": "peer-container",
          "mount_root_closed": true,
          "evidence": true,
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
          "hostname": "0315ea32389b",
          "own": {
            "path": "/pod-cgroup/docker/0315ea32389bc1f5ac4ae52379ea186022dbe241656afab580a2c5655ed1cf28",
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
          "peer_containers": [
            {
              "path": "/pod-cgroup/docker/0e3d3ae4e6e52750f82c78a069e4306e086d938d0f6810d5747ee48d3ef33d91",
              "owner": {
                "uid": 65534,
                "gid": 65534,
                "mode": "0o755"
              },
              "delegated_to_our_uid": true,
              "cpu_max": "EACCES",
              "cgroup_procs": "WRITABLE",
              "subtree_control": "WRITABLE",
              "mkdir": "WRITABLE"
            },
            {
              "path": "/pod-cgroup/docker/2668a85ac283c423b406054f3a64f5588a822bcca716dec3517bccf13be9927e",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/31bf2ab1609c4510882b920115f9e360eec53681d25962e535527b2a93f71cd6",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/4266be855b7cb46812d6df1da5bc591e9c3f5b08c87e1832939b48051b857d9d",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/50f39f34350fbec9261df88c4ef276ba6f71ca491d5dda5b6cb29965dc31f935",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/55da732083d6c8b949367222b98e580dbef63d654d9e062f7af1229bfed6bef0",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8",
              "owner": {
                "uid": 65534,
                "gid": 65534,
                "mode": "0o755"
              },
              "delegated_to_our_uid": true,
              "cpu_max": "EACCES",
              "cgroup_procs": "WRITABLE",
              "subtree_control": "WRITABLE",
              "mkdir": "WRITABLE"
            },
            {
              "path": "/pod-cgroup/docker/652abcef44b8e6e492179ac636d2233429d1582879110d668d700cee458336c0",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/69914a17e95613bd79573d13f82dfb2f841c400e1620186dc7e725740044b02d",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/9f6ba4a0c1a9b29c6555120bac22fbbe2938cb12e13581aa6dff07b89ac8410b",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/b8e70c8de35d6097a8290b29256f0ce2836dc18f984a6e05106dfdbc06457d08",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/buildkit",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/buildx",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/d891595d5af4caba9f1756870793044d04dd6aee97b38716eb275471d234bb56",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/df12053355f0d6fda015353119a675cb7044819ab010c93b551293d1e28d3398",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            }
          ],
          "mount_root": {
            "path": "/pod-cgroup",
            "owner": {
              "uid": 0,
              "gid": 0,
              "mode": "0o755"
            },
            "has_cpu_max_and_procs": true,
            "cpu_max": "EACCES",
            "cgroup_procs": "EACCES",
            "mkdir": "EACCES"
          },
          "own_subtree": {
            "own": "/pod-cgroup/docker/0315ea32389bc1f5ac4ae52379ea186022dbe241656afab580a2c5655ed1cf28",
            "children": [
              "worker"
            ],
            "proc_self_cgroup": "0::/worker"
          },
          "own_delegated": true,
          "peer_containers_count": 15,
          "foreign_peers": [
            "/pod-cgroup/docker/2668a85ac283c423b406054f3a64f5588a822bcca716dec3517bccf13be9927e",
            "/pod-cgroup/docker/31bf2ab1609c4510882b920115f9e360eec53681d25962e535527b2a93f71cd6",
            "/pod-cgroup/docker/4266be855b7cb46812d6df1da5bc591e9c3f5b08c87e1832939b48051b857d9d",
            "/pod-cgroup/docker/50f39f34350fbec9261df88c4ef276ba6f71ca491d5dda5b6cb29965dc31f935",
            "/pod-cgroup/docker/55da732083d6c8b949367222b98e580dbef63d654d9e062f7af1229bfed6bef0",
            "/pod-cgroup/docker/652abcef44b8e6e492179ac636d2233429d1582879110d668d700cee458336c0",
            "/pod-cgroup/docker/69914a17e95613bd79573d13f82dfb2f841c400e1620186dc7e725740044b02d",
            "/pod-cgroup/docker/9f6ba4a0c1a9b29c6555120bac22fbbe2938cb12e13581aa6dff07b89ac8410b",
            "/pod-cgroup/docker/b8e70c8de35d6097a8290b29256f0ce2836dc18f984a6e05106dfdbc06457d08",
            "/pod-cgroup/docker/buildkit",
            "/pod-cgroup/docker/buildx",
            "/pod-cgroup/docker/d891595d5af4caba9f1756870793044d04dd6aee97b38716eb275471d234bb56",
            "/pod-cgroup/docker/df12053355f0d6fda015353119a675cb7044819ab010c93b551293d1e28d3398"
          ],
          "foreign_peers_closed": true,
          "every_peer_cpu_max_closed": true,
          "delegated_peers": [
            {
              "path": "/pod-cgroup/docker/0e3d3ae4e6e52750f82c78a069e4306e086d938d0f6810d5747ee48d3ef33d91",
              "cgroup_procs": "WRITABLE",
              "subtree_control": "WRITABLE",
              "mkdir": "WRITABLE",
              "cpu_max": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8",
              "cgroup_procs": "WRITABLE",
              "subtree_control": "WRITABLE",
              "mkdir": "WRITABLE",
              "cpu_max": "EACCES"
            }
          ],
          "peer_visible": true,
          "mount_looks_narrowed": false,
          "check4_mode": "peer-container",
          "mount_root_closed": true,
          "evidence": true,
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
          "hostname": "0e3d3ae4e6e5",
          "own": {
            "path": "/pod-cgroup/docker/0e3d3ae4e6e52750f82c78a069e4306e086d938d0f6810d5747ee48d3ef33d91",
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
          "peer_containers": [
            {
              "path": "/pod-cgroup/docker/0315ea32389bc1f5ac4ae52379ea186022dbe241656afab580a2c5655ed1cf28",
              "owner": {
                "uid": 65534,
                "gid": 65534,
                "mode": "0o755"
              },
              "delegated_to_our_uid": true,
              "cpu_max": "EACCES",
              "cgroup_procs": "WRITABLE",
              "subtree_control": "WRITABLE",
              "mkdir": "WRITABLE"
            },
            {
              "path": "/pod-cgroup/docker/2668a85ac283c423b406054f3a64f5588a822bcca716dec3517bccf13be9927e",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/31bf2ab1609c4510882b920115f9e360eec53681d25962e535527b2a93f71cd6",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/4266be855b7cb46812d6df1da5bc591e9c3f5b08c87e1832939b48051b857d9d",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/50f39f34350fbec9261df88c4ef276ba6f71ca491d5dda5b6cb29965dc31f935",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/55da732083d6c8b949367222b98e580dbef63d654d9e062f7af1229bfed6bef0",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8",
              "owner": {
                "uid": 65534,
                "gid": 65534,
                "mode": "0o755"
              },
              "delegated_to_our_uid": true,
              "cpu_max": "EACCES",
              "cgroup_procs": "WRITABLE",
              "subtree_control": "WRITABLE",
              "mkdir": "WRITABLE"
            },
            {
              "path": "/pod-cgroup/docker/652abcef44b8e6e492179ac636d2233429d1582879110d668d700cee458336c0",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/69914a17e95613bd79573d13f82dfb2f841c400e1620186dc7e725740044b02d",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/9f6ba4a0c1a9b29c6555120bac22fbbe2938cb12e13581aa6dff07b89ac8410b",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/b8e70c8de35d6097a8290b29256f0ce2836dc18f984a6e05106dfdbc06457d08",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/buildkit",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/buildx",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/d891595d5af4caba9f1756870793044d04dd6aee97b38716eb275471d234bb56",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/df12053355f0d6fda015353119a675cb7044819ab010c93b551293d1e28d3398",
              "owner": {
                "uid": 0,
                "gid": 0,
                "mode": "0o755"
              },
              "delegated_to_our_uid": false,
              "cpu_max": "EACCES",
              "cgroup_procs": "EACCES",
              "subtree_control": "EACCES",
              "mkdir": "EACCES"
            }
          ],
          "mount_root": {
            "path": "/pod-cgroup",
            "owner": {
              "uid": 0,
              "gid": 0,
              "mode": "0o755"
            },
            "has_cpu_max_and_procs": true,
            "cpu_max": "EACCES",
            "cgroup_procs": "EACCES",
            "mkdir": "EACCES"
          },
          "own_subtree": {
            "own": "/pod-cgroup/docker/0e3d3ae4e6e52750f82c78a069e4306e086d938d0f6810d5747ee48d3ef33d91",
            "children": [
              "worker"
            ],
            "proc_self_cgroup": "0::/worker"
          },
          "own_delegated": true,
          "peer_containers_count": 15,
          "foreign_peers": [
            "/pod-cgroup/docker/2668a85ac283c423b406054f3a64f5588a822bcca716dec3517bccf13be9927e",
            "/pod-cgroup/docker/31bf2ab1609c4510882b920115f9e360eec53681d25962e535527b2a93f71cd6",
            "/pod-cgroup/docker/4266be855b7cb46812d6df1da5bc591e9c3f5b08c87e1832939b48051b857d9d",
            "/pod-cgroup/docker/50f39f34350fbec9261df88c4ef276ba6f71ca491d5dda5b6cb29965dc31f935",
            "/pod-cgroup/docker/55da732083d6c8b949367222b98e580dbef63d654d9e062f7af1229bfed6bef0",
            "/pod-cgroup/docker/652abcef44b8e6e492179ac636d2233429d1582879110d668d700cee458336c0",
            "/pod-cgroup/docker/69914a17e95613bd79573d13f82dfb2f841c400e1620186dc7e725740044b02d",
            "/pod-cgroup/docker/9f6ba4a0c1a9b29c6555120bac22fbbe2938cb12e13581aa6dff07b89ac8410b",
            "/pod-cgroup/docker/b8e70c8de35d6097a8290b29256f0ce2836dc18f984a6e05106dfdbc06457d08",
            "/pod-cgroup/docker/buildkit",
            "/pod-cgroup/docker/buildx",
            "/pod-cgroup/docker/d891595d5af4caba9f1756870793044d04dd6aee97b38716eb275471d234bb56",
            "/pod-cgroup/docker/df12053355f0d6fda015353119a675cb7044819ab010c93b551293d1e28d3398"
          ],
          "foreign_peers_closed": true,
          "every_peer_cpu_max_closed": true,
          "delegated_peers": [
            {
              "path": "/pod-cgroup/docker/0315ea32389bc1f5ac4ae52379ea186022dbe241656afab580a2c5655ed1cf28",
              "cgroup_procs": "WRITABLE",
              "subtree_control": "WRITABLE",
              "mkdir": "WRITABLE",
              "cpu_max": "EACCES"
            },
            {
              "path": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8",
              "cgroup_procs": "WRITABLE",
              "subtree_control": "WRITABLE",
              "mkdir": "WRITABLE",
              "cpu_max": "EACCES"
            }
          ],
          "peer_visible": true,
          "mount_looks_narrowed": false,
          "check4_mode": "peer-container",
          "mount_root_closed": true,
          "evidence": true,
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
      "sandbox_id": "sbx_b6b96c66428cc73a",
      "node_id": "worker-1",
      "cgroup": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8/sbx_sbx_b6b96c66428cc73a"
    },
    {
      "sandbox_id": "sbx_cf07ca4d1528b6a8",
      "node_id": "worker-3",
      "cgroup": "/pod-cgroup/docker/0e3d3ae4e6e52750f82c78a069e4306e086d938d0f6810d5747ee48d3ef33d91/sbx_sbx_cf07ca4d1528b6a8"
    },
    {
      "sandbox_id": "sbx_dd3539a9d7259101",
      "node_id": "worker-2",
      "cgroup": "/pod-cgroup/docker/0315ea32389bc1f5ac4ae52379ea186022dbe241656afab580a2c5655ed1cf28/sbx_sbx_dd3539a9d7259101"
    },
    {
      "sandbox_id": "sbx_d317e4bf714d8792",
      "node_id": "worker-1",
      "cgroup": "/pod-cgroup/docker/62bd581260362ded7f513ff762badceba3a877372d2add259c41258f061c53a8/sbx_sbx_d317e4bf714d8792"
    }
  ],
  "started_at": "2026-10-06T17:50:56+0800",
  "elapsed_s": 98.8,
  "ok": true
}
```

## 5. RED 原始读数（`E2B_SANDBOX_CGROUP=off`）

```json
{
  "lane": {
    "api_url": "http://127.0.0.1:3250",
    "internal_url": "http://control-plane:3000",
    "worker_exec_template": "docker exec -i n83acc2-{node}-1 bash -lc",
    "nodes": [
      "worker-1",
      "worker-2",
      "worker-3"
    ],
    "cgroup_mount": "/pod-cgroup",
    "template": "base",
    "flood_seconds": 40.0,
    "worker_env": {
      "worker-1": {
        "pid1": "python",
        "observed": {
          "E2B_SANDBOX_CGROUP": {
            "value": "off",
            "source": "/proc/1/environ"
          },
          "E2B_CGROUP_MOUNT": {
            "value": "/pod-cgroup",
            "source": "/proc/1/environ"
          },
          "E2B_SANDBOX_NOTIFY_RATE_LIMIT": {
            "value": "0",
            "source": "/proc/1/environ"
          }
        }
      },
      "worker-2": {
        "pid1": "python",
        "observed": {
          "E2B_SANDBOX_CGROUP": {
            "value": "off",
            "source": "/proc/1/environ"
          },
          "E2B_CGROUP_MOUNT": {
            "value": "/pod-cgroup",
            "source": "/proc/1/environ"
          },
          "E2B_SANDBOX_NOTIFY_RATE_LIMIT": {
            "value": "0",
            "source": "/proc/1/environ"
          }
        }
      },
      "worker-3": {
        "pid1": "python",
        "observed": {
          "E2B_SANDBOX_CGROUP": {
            "value": "off",
            "source": "/proc/1/environ"
          },
          "E2B_CGROUP_MOUNT": {
            "value": "/pod-cgroup",
            "source": "/proc/1/environ"
          },
          "E2B_SANDBOX_NOTIFY_RATE_LIMIT": {
            "value": "0",
            "source": "/proc/1/environ"
          }
        }
      }
    }
  },
  "n82_baseline": {
    "ops_per_s": 18149,
    "cores_on_worker_pod": 1.02
  },
  "checks": {
    "1_quota_is_real": {
      "pass": false,
      "declared_cpu_percent": 100.0,
      "measured_cpu_percent": 400.5209329289281,
      "cpu_max_readback": null,
      "sandbox_cgroup": null,
      "spinner_node": "worker-1",
      "first_sandbox_rtt_quiet": {
        "samples_ms": [
          423.66,
          33.65,
          40.3,
          49.91,
          27.45
        ],
        "min_ms": 27.45,
        "median_ms": 40.3
      },
      "second_sandbox_rtt": {
        "samples_ms": [
          231.37,
          99.89,
          82.84,
          67.02,
          26.84
        ],
        "min_ms": 26.84,
        "median_ms": 82.84
      },
      "round_trip_criterion": "min-of-5 neighbour <= 3x the quiet min-of-5 (floor 50 ms) -- detects gross starvation (the N82 shape stalled 860 ms); a subtle slowdown is below its resolution and is caught by check 3's cgroup accounting instead",
      "round_trip_bound_ms": 82.35,
      "second_sandbox_node": "worker-1",
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
        "probe_output": "DONE op=openclose stalls=0 rounds=184 elapsed_s=40.1 ops_per_s=9185",
        "ops_per_s": 9185,
        "elapsed_s": 42.7,
        "sandbox_id": "sbx_1d5de4ed45d757f5",
        "node_id": "worker-1",
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
      "criterion": "own delegated cgroup writable (cgroup.procs/subtree_control, NOT cpu.max); every visible peer container's cpu.max still EACCES; every non-delegated peer's cpu.max/cgroup.procs/mkdir EACCES",
      "workers": {
        "worker-1": {
          "error": "worker command on worker-1 exited 1: cannot pick this worker's delegated container cgroup under /pod-cgroup: 0 candidates, 0 matching hostname c312784f3dd1: []"
        },
        "worker-2": {
          "error": "worker command on worker-2 exited 1: cannot pick this worker's delegated container cgroup under /pod-cgroup: 0 candidates, 0 matching hostname 9e0bd5279d4d: []"
        },
        "worker-3": {
          "error": "worker command on worker-3 exited 1: cannot pick this worker's delegated container cgroup under /pod-cgroup: 0 candidates, 0 matching hostname a3cae77871ba: []"
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
      "sandbox_id": "sbx_0d20735e730a6a16",
      "node_id": "worker-1",
      "cgroup": null
    },
    {
      "sandbox_id": "sbx_72c97a64af3cca4d",
      "node_id": "worker-3",
      "cgroup": null
    },
    {
      "sandbox_id": "sbx_4ad9613a751f665e",
      "node_id": "worker-2",
      "cgroup": null
    },
    {
      "sandbox_id": "sbx_f9d9595c5c8a8962",
      "node_id": "worker-1",
      "cgroup": null
    }
  ],
  "started_at": "2026-10-06T17:47:58+0800",
  "elapsed_s": 77.7,
  "ok": false
}
```

## 6. RED 的补充读数：这笔 CPU 记在**谁**头上

同一条 `openclose` 洪泛（限流关、**没有**每沙箱 cgroup）期间，每 2 s 采三台 worker **自己容器
cgroup** 的 `cpu.stat`（一次性探针 `tmp/n83acc2-red-neighbour-cpu.py`，未入库）：

```
worker-1: peak 0.869 cores, median 0.860 cores
worker-2: peak 0.030 cores, median 0.024 cores
worker-3: peak 0.032 cores, median 0.027 cores
```

即：洪泛那 ~0.87 核**记在 hosting worker（本轮是 worker-1）的容器 cgroup 上**，租户账上是 0 —— 与 N82
（`docs/open-issues.md` N82：18149 op/s、1.02 核记在 worker pod）同形。GREEN 档里同一笔 CPU
落在 `sbx_<id>` 子树里。

这一趟探针自己的输出是 `DONE op=openclose stalls=0 rounds=191 elapsed_s=40.0 ops_per_s=9545`；
完整 JSON（含每个采样点的 `usage_usec`）见工作树 `tmp/n83acc2-red-neighbour-cpu.json`。

## 7. 五条检查逐条判定

| # | 判据（脚本里的实际条件） | GREEN 读数 | 判定 |
|---|---|---|---|
| ① 额度是真的 | `measuredCpuPercent ∈ [50,150]`（声明 100）且同节点第二箱往返 min-of-5 ≤ 3× 静默 min-of-5（下限 50 ms） | `100.16`；`cpu_max=100000 100000`；第二箱 **31.15 ms** vs 静默 **39.65 ms**（上限 118.95 ms） | PASS |
| ② 内核在强制 | 3 s 自旋窗口内 `nr_throttled_delta > 0` 且 `observed_cores ∈ [0.5,1.5]×quota` | `usage_usec +3,201,252`、`nr_throttled +32`、`throttled_usec +9,588,263`、`1.022 核` | PASS |
| ③ 洪泛花自己的额度 | 探针单跑 `peak ≤ 1.15×quota` **且** 四路并发 `peak ≤ 1.15×quota` **且** `nr_throttled > 0` **且**两次都真拿到 `ops_per_s` | 单独 `9,794 op/s / 0.840 核 / nr_throttled 0`；四路 `11,945 op/s（2986×4）/ 0.994 核 / nr_throttled +12` | PASS |
| ④ 收窄/视图形状 | 三台 worker：自家容器 cgroup 属主 65534 且 `cgroup.procs`/`subtree_control` 可写、`cpu.max` 不可写；**同层每个 peer 容器**的 `cpu.max` 不可写；非被委派的 peer 的 `cpu.max`/`cgroup.procs`/`mkdir` 全不可写；（peer 全不可见时）挂载根的 `cpu.max`/`cgroup.procs`/`mkdir` 三条也都不可写 | 三台全满足（`check4_mode = peer-container`，每台 **15 个 peer**）：外来 peer（root 所有）三写全 `EACCES`；被委派 peer（同 uid 的另两台 worker）只有 `cpu.max` 是 `EACCES`，`cgroup.procs`/`subtree_control`/`mkdir` 按 uid 可写（逐条记在 `delegated_peers`）；挂载根 `/pod-cgroup` 三写全 `EACCES` | PASS |
| ⑤ 负例 fail-closed | 三台 worker 自家 `cpu.max` 的 `open(O_WRONLY)` == `EACCES` | worker-1/2/3 全 `EACCES` | PASS |

RED 档同一条脚本的判定（**这就是"不弱化"的证据**：RED 五条全 FAIL，且每条都给了具名理由）：

| # | RED 读数 | 判定 |
|---|---|---|
| ① | `measuredCpuPercent = 400.52`、`sandbox_cgroup = null`（邻居 min 26.84 ms vs 静默 27.45 ms —— 见 §11 的说明） | FAIL |
| ② | `reason: there is no sbx_<id> cgroup … (E2B_SANDBOX_CGROUP off?)` | FAIL |
| ③ | 探针 `9185 op/s`，`peak_cores = null`；binding harness 具名拒绝（找不到 `sbx_<id>`） | FAIL |
| ④ | 没有"自家被委派目录"（视图里没有 `worker/` 子树） | FAIL |
| ⑤ | 没有被委派的容器目录 ⇒ 三台都判不出"自家 cgroup" | FAIL |

## 8. 没做 / 没验证的（如实说）

* **线上（k0s）没滚**：Task 7 的 Step 3（两次 apply）与线上复验（k8s 版 ④ + 端到端冒烟）
  **没做**，因为这个任务的范围到"本地 lane 全绿 + 文档 + 回退杆"。上线剧本写在
  `docs/deploy-clusters.md` §7.48，回退杆也在那里。
* **内存/进程数**（`memory.max` / `pids.max`）是 Phase 2，本任务不涉及（计划 D6）。
* **多节点跨 worker 的邻居保护**：只量了"同节点第二箱的往返不掉速"这一条，节点级公平（CFS 层次带宽）
  没有单独测量；而且这条 rtt 判据**只能发现粗粒度饿死** —— RED 档（4 自旋、无 cgroup）量到邻居
  26.84 ms vs 静默 27.45 ms，**确实没掉速**，所以它在这条车道上证明不了"邻居没被吵"；那件事的正面
  证据是 ③ 的额度记账。详见 §11。
* **`E2B_SANDBOX_NOTIFY_RATE_LIMIT=0` 只在这次验收的栈里**：生产清单没改，N83 Phase 1 的
  Task 8（限流器降级）还没有做。
* 本机 Docker VM 是 **cgroup v2 / x86_64**（`docker info`：`CgroupVersion 2`、`Architecture x86_64`、
  6 核 / 20 GB）。k0s 节点是 arm64 的 4 核，**镜像形状与 cgroup 行为在那边仍需按 Step 3 复验**。
* 一次"额度先被自家进程占满、再往这个沙箱排新命令"的读数：命令队列 **30 s 超时**
  （RPC `RateLimitException: command queue timed out after 30s`）。这被我在脚本里如实处理
  （binding harness 改成"一条命令里起四个客户端"），并在 §7.48 的坑 3 里记下。

## 9. 收尾

### 9.1 单元 / 钉子测试（本机 macOS）

> 下面这一段是 Task 7 那一趟（`95907a1`/`164c987`）的记录；**acceptance-final 复跑没有重跑整套单测**
> （本轮范围是五条检查 + 归档刷新），只重跑了文档钉子 `tests/unit/test_docs_only_point_at_repo_artifacts.py`，
> 结果与下面这批 macOS 环境红**同数同类**（`2 failed, 7 passed`，红的正是
> `test_every_live_doc_tmp_reference_is_allowed_with_a_reason` /
> `test_no_live_doc_names_a_script_that_this_repo_does_not_have`，指向
> `tmp/clone3_probe.py` / `tmp/userns_thread_probe.py` / `identity_grant.py` 这几处**与本改动无关**的既有
> 引用）。本轮其余验证是脚本自身的退出码与 §4/§5 的两份 JSON 读数。

`python -m pytest tests/unit -q` ⇒ **2562 passed, 13 skipped, 15 failed**。这 15 条与我的改动无关：
把同一个 venv 指向 `95907a1` 的干净 worktree（`git worktree add --detach tmp/head-check 95907a1`）
重跑同一组用例，**失败集合逐条相同**（`test_apply_requires_cluster_guard` 5、
`test_c2_p0_probe` 2、`test_docs_only_point_at_repo_artifacts` 2、`test_migrate_state_base_script` 3、
`test_real_root_gate` 1、`test_xfs_quotactl_backend` 2 —— 就是 HANDOFF 记的那批 macOS 环境红）。
与 Task 7 直接相关的两条也单独跑过并绿：`tests/unit/test_worker_manifest_permissions.py`（68 passed 那批）、
`tests/unit/test_worker_env_key_sets.py` + `test_compose_base_image_shape.py` + `test_cluster_guard.py` +
`test_secret_scripts_require_cluster_guard.py`（27 passed）。

* 拆栈后核对（Task 7 那趟）：`docker compose -p n83acc ps` 空、`GET /sandboxes` = `[]`、
  `ls -d /pod-cgroup/docker/*/sbx_*`（三台 worker）= 无输出。
* **acceptance-final 复跑（`d600b7e`）的收尾**：拆栈前 `GET /sandboxes` = `[]`、三台 worker 上
  `ls -d /pod-cgroup/docker/*/sbx_*` 各 = `0`；`docker compose -p n83acc2 … down` 之后
  `docker ps -a --filter name=n83acc2` = **0 个容器**（两个项目网络也清掉，三个项目卷留下），并用一个
  `--rm` 容器把 VM 的 `/sys/fs/cgroup` 只读挂进来复核：59 个 `docker/*` 容器 cgroup、**`sbx_* = 0`**。
* 用户的 live 栈（project `compose`、3100）自始至终是 `Up`，镜像/容器未被触碰。

## 10. 与计划的偏差（都要有人知道）

1. **执行者与立项文本不同**：计划 §3.2 定的是"worker 自管、agent 只做一次性委派"（形态 W），
   N83 行里那段"由 agent 建子 cgroup"是**更早的 D2/D3 版本**；我按 §3.2 实现与验收，并把
   N83 行的那段标成"原始立项文本"。
2. **check ③ 加了第二条读数**：计划只要求"洪泛仍落在额度内"（= 单独跑 ≤ 额度），照做是
   `0.84 核 ≤ 1 核`。但那条**没有**证明额度 binding（负载自限在 1 核以下）。所以我在**同一条检查里**
   追加"四路并发"读数（用**探针自己的 INNER 程序**，import 而非抄写）—— `0.994 核` + `nr_throttled +12`
   才真正说明"沙箱自己的额度在 bound 它"。判据里两条都要过。
3. **check ④ 的本地版判据**：计划的写法是"本地 lane = 只看得到自己容器那棵子树"，但 compose 没有
   `subPathExpr`、挂载的是**整棵 VM 树**（§1.4 的探针也这么量过）。所以本地版 ④ 判的是"**同层 peer
   容器**里，我们没被给的那些（root 所有）三写全 EACCES；每个 peer 的 `cpu.max` 一律 EACCES；
   被委派的那两个同 uid peer 如实上报"。k8s 那版（挂载本身收窄、`ls /pod-cgroup` 看不到 `kubepods/`、
   根探针为据）留给 Step 3 复验。fix round 1 之前这里探的其实是**挂载根**、根本没到 peer —— 见 §11。

---

# 附：Fix round 1（2026-10-06，评审回修）

评审结果：**Spec ❌（check ④）+ 4 条 Important + 3 条 minor**。逐条处置如下，全部在同一个 worktree
（`/Users/polus/project/ai/sandlock-e2b.wt/task7`，branch `n83/task7`）里改、**GREEN 与 RED 都重跑**过。
本节引用的每个数字都取自本节归档的那两份 JSON（`tmp/n83-acceptance-green.json` /
`tmp/n83-acceptance-red.json`，与 §4/§5 的代码块逐字一致）。

## F1（Spec ❌ + Important）check ④ 探的不是 peer 容器

**判定：成立，且比评审说的更值得记一笔。**

* 原实现 `_VIEW_SCRIPT` 从**挂载根** `os.walk` 下去，`/pod-cgroup` 自己就有 `cpu.max` ⇒ 第一个命中就是
  根，随后 `dirnames[:] = []` 剪枝 ⇒ **peer 容器一个都没访问到**（GREEN JSON 里 `foreign:[{path:"/pod-cgroup",
  owner:{uid:0}}]` 正是这个）。
* 修法：peer 的定义改成**同层容器目录**（`own.parent` 下 != own、且 `cpu.max` + `cgroup.procs` 都在的
  目录）；挂载根**单独探**。归档的那一版 JSON 里这个根探针带一个布尔字段（当时叫
  `is_container_cgroup`，fix round 2 已改名为 `has_cpu_max_and_procs`）——它只说明"这个目录带
  `cpu.max` + `cgroup.procs`"，Docker VM 的 cgroup 根两者都有，**它不是容器**；旧名字声称的比探针
  能知道的更多，所以改了名。
* 新读数（GREEN，三台一致）：`peer_containers_count = 15`；`check4_mode = "peer-container"`；
  `foreign_peers_closed = true`（root 所有的控制面/redis/agent/**另一套栈的**容器，三写全 EACCES）；
  `every_peer_cpu_max_closed = true`；`mount_root` 三写全 EACCES。
* **顺带量到、必须写下来的真实现象**：peer 里有 **2 个是"被委派"的**（同宿主的另两台 worker，
  委派把它们的容器 cgroup chown 给了 65534）。对这 2 个 peer：
  `cpu.max = EACCES`（委派故意不含它，⑤ 钉的正是这条），但
  `cgroup.procs` / `subtree_control` / `mkdir = WRITABLE`——**同一个宿主 uid 的 DAC 分不开两个容器**。
  这是 **compose 车道上这次委派自己带来的性质**：N83 的委派把 peer 容器 cgroup 的**目录** chown 给
  65534，而三台 worker 又**共用同一个宿主 uid**，所以对另两台 worker 的容器 cgroup
  `cgroup.procs`/`subtree_control`/`mkdir` 可写（`cpu.max` 仍 root 所有 —— 委派故意不含它）。它
  **不是**与 N83 无关的既有形状；k8s 车道因为 `subPathExpr` 把挂载收窄到本 pod，peer 根本不可达，
  不存在这个形状。脚本把它**逐 peer 上报**（`delegated_peers`），不隐藏、也不当作"通过"。
* 因此 ④ 的通过条件（脚本里逐字）：
  `own_delegated AND every_peer_cpu_max_closed AND evidence`，其中 `evidence` =
  有非被委派 peer 时要求它们三写全闭；peer 全不可见时（k8s 收窄形状）退化为"挂载根必须不可写**且**
  挂载看起来是收窄的"。两种形状都写进 JSON（`check4_mode` / `evidence` / `mount_root_closed`）。
* 文档同步：`docs/deploy-clusters.md` §7.48 的 ④ 行、坑 4、以及本报告 §7/§10 已按上面的**实际读数**重写。

## F2（Important）车道元信息是写死的字符串

**判定：成立。** 原 `report["lane"]["sandbox_cgroup_env"]` 是常量 `"required (…)"`，RED 档（`off`）
也照印 `required`。

* 修法：新增 `lane_env()` —— 在**每个 worker 容器内部**读 `E2B_SANDBOX_CGROUP` / `E2B_CGROUP_MOUNT` /
  `E2B_SANDBOX_NOTIFY_RATE_LIMIT`，来源是 **`/proc/1/environ`**（worker 进程自己的环境），读不到才退到
  exec 的环境，两者都没有就记 `null` + `source: "unset"`；连同 PID 1 的 cmdline 一起按 worker 上报
  （`lane.worker_env`）。写死的两个字段已从脚本里删除。
* 重跑后的读数（归档 JSON 里可直接核对）：
  * GREEN：三台都 `{"E2B_SANDBOX_CGROUP": {"value": "required", "source": "/proc/1/environ"}, …}`
  * RED：三台都 `{"value": "off", …}` —— 归档件与那一趟实际开关**一致**了。

## F3（Important）① 的邻居 rtt 上限是退化的

**判定：成立。** 旧式 `max(2×quiet, 200ms)` 在本车道的 quiet（几十毫秒）下就等于 200 ms（≈6×），RED 的邻居
（26.84 vs 27.45 ms）也能过 —— 这条子判据测不出它声称的东西。

* 修法：上限改成 **`3.0 × 静默 min-of-5`，下限 50 ms**（常量 `_NEIGHBOUR_RTT_FACTOR` /
  `_NEIGHBOUR_RTT_FLOOR_MS`），并把判据字符串与 `round_trip_bound_ms` 一起写进 JSON。
  50 ms 的理由：观测到的抖动只有几 ms，而 N82 那种一秒悬崖是 860 ms ⇒ 3× 能抓住粗粒度饿死、
  又留了 >10× 的余量。
* **RED 档的实话**：`27.45 ms（静默）→ 26.84 ms（邻居，同节点、另一箱在跑 4 自旋时）` ——
  这条车道上**邻居确实没有掉速**，所以这条子判据在本车道**无法**证明"邻居没被吵"，它只能发现
  粗粒度饿死（860 ms 级）。这一点已写进 ① 的判据字符串、`docs/deploy-clusters.md` §7.48 的 ① 行
  与 §8；"邻居不被吵"的正面证据是 ③ 的额度记账（CPU 落在沙箱自己的 cgroup 上、超了被节流），
  不是这条 rtt。**没有把上限放宽来换取通过。**

## F4（Important）文档里的 k0s"只换两个参数"是错的

**判定：成立。** `--internal-url`（默认 `http://control-plane:3000`）、`--api-key`（默认 `local-key`）、
`--internal-key`（默认 `internal-key`）都是 compose 专用值。

* 修法：脚本 docstring 列全**五个**参数并给了 k0s 的取值来源；`docs/deploy-clusters.md` §7.48 的
  开头段与"怎么再跑一遍"都改成"车道相关的一共五个、都得给"，并指向 docstring 里的完整命令。

## Minors

1. **`docs/env-vars.md` 的额度写法**：`cpu.max = cpu_count×1000 100000` → 改成
   `cpu_percent×1000 100000`，并写明 `cpu_percent` 是**一个核的百分比**、今天 `cpu_count` 恒为 1、
   默认 100% 就是 `100000 100000`（含 supervisor）。
2. **数字与归档件不一致**（RED ③ 的 op/s、§10 里那处四路并发的旧读数）：两份归档 JSON 用**同一支
   修好的脚本**重跑后重新取自 JSON —— GREEN `100.16 / +3,201,252 / nr_throttled +32 /
   9,794 op/s / 0.840 核 / 11,945 op/s（2986×4）/ 0.994 核 / +12`；RED `400.52 / 9185 op/s`。
   `docs/deploy-clusters.md` §7.48、`docs/open-issues.md` 的 N83 行与引用这些读数的表全部取自这两份
   JSON。**但 §10 第 2 条当时漏改了**，还带着 round 0 留下的核数与节流计数（这里不重复抄旧值）——
   评审指出后已在 **fix round 2** 改成 `0.994 核` / `nr_throttled +12`；原先"全部对齐"的说法过强，
   已收敛为上句那种逐处可核对的说法。
3. **回退杆写死行号**：`docs/deploy-clusters.md` §7.48 与 `docs/open-issues.md` 都写明是
   `deploy/k8s-k0s/worker-capacity.patch.yaml:41`（`- name: E2B_SANDBOX_CGROUP`，第 42 行是 value）。

## 没动的裁定

* k0s 车道仍然**不在本轮范围**（一个 pod 都没碰）；脚本仍然是对任意 endpoint 可跑（只是参数变多）；
  ③ 的四路并发读数保留（它是"额度真的 binding"的证据）；用户的 live 栈（project `compose`、3100）
  本轮同样全程未动 —— 用的是我自己的 `-p n83acc` 栈，验收完已拆。

## 本轮的执行与复验（命令与结果）

```bash
# 1) 起 GREEN 档（我自己的栈，端口 3200）
docker compose -p n83acc -f deploy/compose/docker-compose.multinode.yml -f tmp/n83-acc-override.yml up -d
# 2) GREEN 验收（exit 0，五条全 PASS）
E2B_API_KEY=local-key tmp/venv/bin/python deploy/scripts/acceptance/cgroup_acceptance.py \
    --api-url http://127.0.0.1:3200 --api-key local-key --internal-key internal-key \
    --internal-url http://control-plane:3000 --nodes worker-1,worker-2,worker-3 \
    --worker-exec-template 'docker exec -i n83acc-{node}-1 bash -lc' \
    --out tmp/n83-acceptance-green.json
# 3) 切 RED 档并跑同一条命令（exit 1，五条全 FAIL，① 的 measuredCpuPercent = 400.52）
docker compose -p n83acc -f deploy/compose/docker-compose.multinode.yml -f tmp/n83-acc-override-off.yml up -d
#    → tmp/n83-acceptance-red.json
# 4) 复位 GREEN 档
docker compose -p n83acc -f deploy/compose/docker-compose.multinode.yml -f tmp/n83-acc-override.yml up -d
```

归档的原始读数就是 §4（GREEN）与 §5（RED）那两个 JSON 代码块（本轮重跑后已替换为最新一版）。

---

# 附：Fix round 2（2026-10-06，收尾轮）

范围只有四条残留（数字一致性、两处措辞、一个新字段名/断言），**没有重跑验收**（评审也说不必）：
§4/§5 那两份归档 JSON **一字未动**，本节说的每一处改动都不改读数、不改任何检查的判决。

## R1（必须改）§10 那处四路并发读数与归档 JSON 对不上

* **改了什么**：§10 第 2 条那处四路并发的读数还是 **round 0 留下的旧值**（旧的核数与节流计数），
  已换成归档 GREEN JSON 里的 **`0.994 核` / `nr_throttled +12`**；§11 里"本节、§4/§5 的 JSON、
  §7/§8/§10 的正文……全部对齐（逐条 grep 核对过）"这句**过强**的说法已删掉，改成逐处可核对的说法，
  并**明写**"§10 第 2 条当时漏改了，fix round 2 才改过来"（旧值本身不再抄进文件 —— 抄一遍就等于把
  错数字又留在 artifact 里）。
* **全 artifact 扫了一遍**（脚本化，见下面"本轮的核对命令与结果"）：评审点名的四个旧数
  （round 0 的 measuredCpuPercent、两次 RED 探针 op/s、round 1 的另一种写法）**均已 0 处命中**，
  连同 round 0 那批时效性读数（旧的 usage/throttled 计数）一起确认清干净了；
  这里只写"已 0 处命中"，不把旧值再抄一遍 —— 抄一遍就等于把错数字又留在文件里。
* 现在引用本轮读数的段落只剩三处载体：本报告 §7 表、`docs/deploy-clusters.md` §7.48 的 RED→GREEN 表、
  `docs/open-issues.md` 的 N83 行 —— 每个数字都来自归档的那两份 JSON（下面有核对脚本的输出）。

## R2（措辞）docstring 里"peer 不可写"与"被委派的 peer 可写"两处打架

* 修法：check ④ 的 docstring 现在**在一处**把规则讲完：**一个 cgroup 可写，当且仅当那次一次性委派把它
  交给了本 worker 的 uid** —— 自己的容器 cgroup，以及"所有 worker 共用同一个宿主 uid"的车道上
  **别人（另几台 worker）的容器 cgroup**；**`cpu.max` 永远不可写**（peer 与自己一视同仁，委派故意不含
  它）；视野里其余每一个 cgroup（root 所有的容器、挂载根本身）**每一条写都是 EACCES**。
  随后才分四小条列 check ④ 到底断言什么（own / 每个 peer 的 cpu.max / 非被委派 peer 的三条写 /
  没有 peer 可见时挂载根的三条写），以及同 uid 那些 peer 用 `delegated_peers` 如实上报。

## R3（措辞）"不是 N83 引入的回归"不准确

* 修法：`docs/deploy-clusters.md` §7.48 坑 4 改成：这是**compose 车道上这次委派自己带来的性质** ——
  委派把 peer 容器 cgroup 的**目录** chown 给 65534，**共享 uid 才使这条委托对别的 worker 可用**；
  范围被两条边界卡住（只有跑在 worker uid 下的进程能用它、只在"整棵树可见"的挂载上成立），
  k8s 车道挂载被 `subPathExpr` 收窄、peer 不可达，**没有这个形状**。不再出现"回归"这种把责任推给
  别处的说法。
* **报告自己也留了一处（final review 追加）**：本文件 §F1 那条 bullet 当时仍写着"compose 车道的固有
  形状…不是 N83 引入的回归"—— 于是**只读这份归档报告**的读者会看到与本附加节相反的说法。已改成同一
  口径：那个 peer 可写形状来自**这次委派把 peer 目录交给 65534 + 三台共用 uid**，不再说"固有/不是
  回归"。这是计划把本文件引用为 Task 7 证据时必须先消掉的自相矛盾。

## R4（新 Minor）字段名与断言同文档对不上

* `mount_root.is_container_cgroup` → **`mount_root.has_cpu_max_and_procs`**（改名，说它真正量到什么：
  该目录带 `cpu.max` + `cgroup.procs`；Docker VM 的 cgroup 根两者都有，但它**不是容器**）。
* `mount_root_closed` 现在把 **`mkdir`** 也算进断言（`cpu.max` + `cgroup.procs` + `mkdir` 三条写都要
  非 WRITABLE），与"挂载根三条写全 EACCES"的文档说法一致。
* **归档漂移（acceptance-final 复跑后已消除）**：当时 §4/§5 的 JSON 是 round 1 那一版跑出来的，
  里面 `mount_root` 的布尔键还是旧名。这次两处代码改动只动**键名**与那条断言的一个项：不改任何读数、
  不改任何判决 —— ① 那份 JSON 的 `mount_root` 块里 `cpu_max`/`cgroup_procs`/`mkdir` 三项**都是
  `EACCES`**，所以新断言在它的读数上同样成立；② `mount_root_closed` 只在 `narrowed-mount` 分支参与
  判决，而 compose 运行是 `check4_mode = peer-container`、证据来自 `foreign_peers_closed`，**根本没走
  那条分支**。当时**没有重跑**、两份 JSON 未改；**本次 acceptance-final 复跑（`d600b7e`）已重跑
  GREEN**，§4 的 JSON 现在就是那一趟的原始 stdout，`mount_root` 键名已经是
  `has_cpu_max_and_procs`（`check4_mode` 仍是 `peer-container`，判决仍 PASS），这处键名漂移已消除。

## 没动（按评审的"do not change"清单）

五条检查的 pass/fail 语义；邻居上限 `max(3×quiet_min, 50 ms)` 与它"只抓粗粒度饿死"的能力边界说明；
`delegated_peers` 的披露；k0s 不在本轮范围（一个 pod 都没碰）；用户的 live `compose` 栈（3100）——
本轮**没有起任何栈**（纯 artifact/文案修改），自然也没碰它。

## 本轮的核对命令与结果（数字与归档 JSON 的一致性）

```text
# 把「引用读数的三段文字」里的每个数字 token 与两份归档 JSON 的数值集合逐一比对
# （允许值 = JSON 里的数、其保留两位小数的形式，或显式列出的结构性常数）
# scoped = 报告 §7 表 + 本附节 + §7.48 的 RED→GREEN 表 + N83 行 + env-vars 的三行
python3 <上面那个 scoped 脚本>
```

第一遍扫出来的 unmatched 只剩**结构性/历史项**：节号（7.46 / 7.48 / 11 / 06 / 82）、端口
（3000 / 3100 / 3200）、判据区间常数（50,150）、以及 N83 行里 **Phase 0 的历史读数**
（0444 / 1046 / 110307 / 375.5 / 382 / 669 —— 那是 `0.1.0-1046-gd2d669b…` 那一版上线时的现场数，
不是本轮读数）；另有两个与归档 JSON 对不上的值在同一轮改掉：一处的秒级换算改成精确的
`+9,588,263 µs`，另一处旧 quiet 值改成"几十毫秒"这种不带具体数字的说法。改完再跑同一脚本，
**引用的读数一个不差地来自归档 JSON**。全部改动都在 worktree 的同一分支上提交（见下）。
