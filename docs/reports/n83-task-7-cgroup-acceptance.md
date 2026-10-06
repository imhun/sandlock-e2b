# Task 7 报告 —— N83 Phase 1：验收脚本 + 本地车道验收 + 文档 + 回退杆

工作树：`/Users/polus/project/ai/sandlock-e2b.wt/task7`（branch `n83/task7`，起点 = 集成分支
`n83-phase1-cgroup` 的 `95907a1`）。**没有 merge / rebase / push。**

> ## 本轮归档（compose 车道收窄复验）
>
> 本文 §4/§5 的两份 JSON、以及 §0–§10 引用的**每一个读数**，都是在 **`c9b3515`**
> （branch **`n83/compose-narrow`**，工作树 `/Users/polus/project/ai/sandlock-e2b.wt/compose-narrow`，
> 基于集成分支的 `3b711cc`）上，用**同一支** `deploy/scripts/acceptance/cgroup_acceptance.py`、
> 同一条**本地 compose 多节点车道**重跑得到的原始 stdout。车道：项目名 **`n83narrow`**、宿主端口
> **3300**（用户 live 栈 `compose`:3100 全程未动）。命令与 override 见 §2/§3。
>
> **这一轮相对上一轮（`d600b7e`，车道 `n83acc2`/3250）只改一处，但那正是本轮的题目**：compose 车道也把
> worker 的 rw cgroupfs 视图**收窄到自己那一片**（`cgroup_parent: /e2b-${COMPOSE_PROJECT_NAME}-worker-<n>`
> + 同名的、指向这一片的 bind，见 §1），于是 **check ④ 第一次离开 `peer-container`、改走
> `narrowed-mount` 分支**（peer 可见数 0）。
>
> §2/§3 的说明在本轮已重写为本轮的配方。**附录 §F/§R 是各轮评审回修的历史记录**：其中的读数按当时
> 归档原样保留（`95907a1`→`164c987`→`d600b7e` 那几趟），不再随本轮刷新；那几趟的车道是
> `n83acc`/3200 与 `n83acc2`/3250。

## 0. 结论

* 验收脚本在本轮车道上仍然**五条检查全绿**（`"ok": true`，退出码 0，`elapsed_s = 103.3`）。
  **脚本一个字节没改** —— 本轮改的是车道清单。
* **本轮要验的收窄成立**：三台 worker 的 `/pod-cgroup` 里**只有 cgroupfs 文件 + 恰好一个容器目录
  （自己的）**；`docker/`、`kubepods*` 都不存在。`${COMPOSE_PROJECT_NAME}` 实测插值成 `n83narrow`，
  委派回答逐台给出 `containerCgroup=/host-cgroup/e2b-n83narrow-worker-<n>/<container-id>`。
* **check ④ 第一次走 `narrowed-mount` 分支**（此前在这条车道上从未被走到）：三台一致
  `check4_mode = "narrowed-mount"`、`peer_containers_count = 0`、`mount_looks_narrowed = true`、
  `mount_root_closed = true`、`evidence = true`。
* **RED 档（`E2B_SANDBOX_CGROUP=off`）仍然五条全 FAIL 且逐条具名**：`measuredCpuPercent=399.93`、
  沙箱没有 cgroup、洪泛的 0.87 核记在 hosting worker **自己容器的 cgroup** 上 —— N82 的症状在本地
  复现，检查没有静默变绿。
* **跨 worker 的 DoS 随收窄消失**：以前 worker B 的容器 cgroup 里被 peer `mkdir` 留一个目录，会让 B 的
  启动自检（"恰好一个属主是自己的子目录"）**具名拒绝**、永远不 ready。今天 peer 的容器目录根本不在
  worker 的挂载命名空间里。
* **读数移动（`d600b7e` → 本轮）**：GREEN ① `100.16→99.96`、静默 min `39.65→29.35 ms`、邻居 min
  `31.15→31.61 ms`、上限 `118.95→88.05 ms`；② `usage_usec +3,201,252→+3,101,766`、`nr_throttled
  +32→+31`、`throttled_usec +9,588,263→+9,301,890`、`1.022→0.992` 核；③ 单跑 `9,794→9,895 op/s`
  （峰值 `0.840→0.833` 核）、四路 `11,945（2986×4）→12,068（3018×4）`、`0.994→0.989` 核、
  `nr_throttled +12→+13`；④ `peer-container / 15 peer → narrowed-mount / 0 peer`；⑤ `EACCES` 不变。
  RED ① `400.52→399.93`、静默 min `27.45→29.93 ms`、邻居 min `26.84→27.52 ms`；③ 探针 `9185→9125
  op/s`；§6 邻居账 hosting 节点 `worker-1（peak 0.869）→worker-2（peak 0.865）`（哪台 hosting 随调度
  变化，形状不变）。**判决未移动**：GREEN 全 PASS、RED 全 FAIL。

## 1. 本轮改了什么（compose 收窄）

| 文件 | 动作 |
|---|---|
| `deploy/compose/docker-compose.multinode.yml` | 三条 worker 各加 `cgroup_parent: /e2b-${COMPOSE_PROJECT_NAME}-worker-<n>`；把 `- /sys/fs/cgroup:/pod-cgroup` 换成 `- /sys/fs/cgroup/e2b-${COMPOSE_PROJECT_NAME}-worker-<n>:/pod-cgroup`；两处注释改成精确说法（compose **有** `volume.subpath`，但对 `type: bind` **静默无效**、且是解析期插值） |
| `deploy/compose/docker-compose.prod.yml` | 同上。worker-1 是 `&worker` 锚，worker-2/3 是 `<<: *worker` 继承 ⇒ 两条各写一份 `cgroup_parent` + `volumes` **覆盖**（否则会共享 worker-1 的收窄视图） |
| `deploy/stack/docker-compose.prod.yml` | 同上（这份是两条 worker） |
| `tests/unit/test_worker_manifest_permissions.py` | 新增钉子 `test_compose_worker_cgroup_bind_is_exactly_its_own_parent`：**逐车道、逐 worker** 断言 bind 源 == `/sys/fs/cgroup` + 该 service 的 `cgroup_parent`、绝不是裸 `/sys/fs/cgroup`、父切片带 `${COMPOSE_PROJECT_NAME}` 且以服务名结尾、同一文件里两个 worker 不共享父切片。原来的"整棵树 bind"断言（`test_compose_lanes_declare_the_workers_narrowed_cgroup_view`）改成"不是裸 `/sys/fs/cgroup`" |
| `docs/superpowers/plans/2026-10-06-n83-per-sandbox-cgroup.md` | §3.5 新增 `①bis`（compose 的收窄配方 + 实测形状 + 名字为什么必须带 project）；"compose 没有 `subPathExpr`"改写为精确说法（有 `volume.subpath`、对 bind 静默无效、解析期插值）；§5 的"第四条风险：同 uid 的 peer 可写"补一段 re-scope（compose 当天也收窄），**原读数保留** |
| `docs/deploy-clusters.md` | §7.48 加"收窄后复验"一段与**坑 5**（bind subpath 静默无效的实测）；坑 2/坑 4 按新形状标注（历史读数原样保留） |
| `docs/open-issues.md` | N83 行的收窄读数补一句：compose 车道当天也收窄、④ 改走 `narrowed-mount`；"同 uid 的 peer 可写"标注为收窄前的历史读数 |
| `docs/reports/n83-task-7-cgroup-acceptance.md` | 本文件（§0–§10 换成这轮读数；附录保留） |

Task 7 原始那一批改动（脚本 + k8s 清单 + compose 绑定 + 文档）见 git 历史。

## 2. 本轮车道、override 与"不让用户环境受影响"

* 起的是 `deploy/compose/docker-compose.multinode.yml`（3 worker + 控制面 + redis + agent 两面），
  **`-p n83narrow`** ⇒ 容器名 `n83narrow-*`、卷/网络都是独立命名空间。
* **用户那套 live 栈（project `compose`、宿主 3100）全程没动**：没有对它跑过 `up/down/build/stop/rm`，
  没有覆盖 `compose-*` 镜像（本轮的镜像是 `n83narrow-*` 与 `e2b-sandlock-agent:n83narrow`）。收工时它仍是
  `Up`。k0s 集群（`172.18.80.94`/`.80.140`）**一个 pod 都没碰**。
* 本轮的 override（`tmp/n83narrow-override.yml` / `tmp/n83narrow-override-off.yml`，都在 gitignored 的
  `tmp/` 下）：
  * 控制面 `ports: !override ["3300:3000"]`（compose 对 `ports` 是**追加**合并，只写新端口会同时绑
    3100 ⇒ 撞用户栈）；
  * 三台 worker 照抄用户 `tmp/n80/compose-override.yml` 的 `E2B_NODE_DISK_MB: "400000"` /
    `E2B_NODE_PROCESSES: "1024"`（Docker VM 整盘已用 ~111 GB，清单里的 4096 是准入配额 ⇒ 不覆盖就是
    `503 No resources available`）与 `E2B_EXECUTOR: sandlock` / `E2B_PER_SANDBOX_UID: "true"`；
  * `E2B_SANDBOX_NOTIFY_RATE_LIMIT: "0"`（**只在这次验收里**关掉通知限流，量完随栈拆掉 = 撤回）；
  * 本轮开关 `E2B_SANDBOX_CGROUP: "required"`；RED 档是**同一份** override、只把这一个值改成 `"off"`
    （收窄的挂载/cgroup_parent 形状两档都在，RED 证的是"开关关掉后沙箱没有 cgroup"）。
* `deploy/compose/.env` 从 `.env.example` 复制，末尾把 `AGENT_IMAGE` / `AGENTS_IMAGE` 指到**本 worktree
  现构建**的 `e2b-sandlock-agent:n83narrow`（否则 agent 是旧 registry 镜像、`delegate-cgroup` 这个 op
  根本不存在，worker 的启动自检会一直 `delegation-timeout`）。
* `wheels/fork/`（gitignored 的构建产物）从主仓库**只读复制**过来 —— worker 镜像要用它装 sandlock wheel。
* 拆栈与清理（§9.2）：`docker compose -p n83narrow … down -v`（连卷一起删）+ 手工 `rmdir` 掉 Docker 建的
  三个 cgroup 父切片 `/sys/fs/cgroup/e2b-n83narrow-worker-{1,2,3}`（拆栈后它们已经空了）。

## 3. 执行过的命令（按时间顺序）

```bash
# 0) 工作树与基线
cd /Users/polus/project/ai/sandlock-e2b.wt/compose-narrow && pwd && git log --oneline -1 && git status

# 1) 依赖与构建产物
cp -a /Users/polus/project/ai/sandlock-e2b/wheels/fork wheels/fork
uv venv --python 3.14 tmp/venv && uv pip install --python tmp/venv/bin/python "e2b==2.46.0" httpx
cp deploy/compose/.env.example deploy/compose/.env   # 追加 AGENT_IMAGE/AGENTS_IMAGE=e2b-sandlock-agent:n83narrow
docker build -f deploy/docker/Dockerfile.agent -t e2b-sandlock-agent:n83narrow .
docker compose -p n83narrow -f deploy/compose/docker-compose.multinode.yml \
    -f tmp/n83narrow-override.yml build control-plane worker-1 worker-2 worker-3
# 插值自检：cgroup_parent 与 bind 源都展开成 n83narrow
docker compose -p n83narrow -f deploy/compose/docker-compose.multinode.yml config | grep -E 'cgroup_parent|source: /sys/fs/cgroup'

# 2) 起栈 + 等委派（三台各一条 "cgroup lane ready"）
docker compose -p n83narrow -f deploy/compose/docker-compose.multinode.yml -f tmp/n83narrow-override.yml up -d
docker logs n83narrow-worker-{1,2,3}-1 | grep -E 'cgroup lane ready|cgroup delegation answer'
# 例（worker-1）：cgroup delegation answer: containerCgroup=/host-cgroup/e2b-n83narrow-worker-1/<container-id>
#   cgroup lane ready: cgroup ready parent=/pod-cgroup/<container-id> worker_uid=65534 drained=1 subtree_control=cpu

# 3) 视图形状自检（收窄的直接读数）
docker exec n83narrow-worker-1-1 bash -lc 'find /pod-cgroup -maxdepth 1 -mindepth 1 -type d; cat /proc/self/cgroup; cat /pod-cgroup/cpu.max'

# 4) 验收（GREEN）
E2B_API_KEY=local-key tmp/venv/bin/python deploy/scripts/acceptance/cgroup_acceptance.py \
    --api-url http://127.0.0.1:3300 --api-key local-key --internal-key internal-key \
    --internal-url http://control-plane:3000 --nodes worker-1,worker-2,worker-3 \
    --worker-exec-template 'docker exec -i n83narrow-{node}-1 bash -lc' \
    --out tmp/n83narrow-acceptance-green.json          # 本文 §4

# 5) 验收（RED：同一份 override，只把 E2B_SANDBOX_CGROUP 翻成 "off"，重建 worker）
docker compose -p n83narrow -f deploy/compose/docker-compose.multinode.yml -f tmp/n83narrow-override-off.yml up -d
# 同一条验收命令 → tmp/n83narrow-acceptance-red.json（exit 1，五条全 FAIL，理由逐条具名）—— 本文 §5
tmp/venv/bin/python tmp/n83narrow-red-neighbour-cpu.py > tmp/n83narrow-red-neighbour-cpu.json   # §6

# 6) 单元/文档钉子
/Users/polus/project/ai/sandlock-e2b/.venv/bin/python -m pytest tests/unit/test_compose_base_image_shape.py \
    tests/unit/test_worker_manifest_permissions.py tests/unit/test_docs_only_point_at_repo_artifacts.py -q

# 7) 拆栈 + 清理
docker compose -p n83narrow -f deploy/compose/docker-compose.multinode.yml -f tmp/n83narrow-override-off.yml down -v
docker run --rm -v /sys/fs/cgroup:/pc alpine:3.20 sh -lc 'rmdir /pc/e2b-n83narrow-worker-1 /pc/e2b-n83narrow-worker-2 /pc/e2b-n83narrow-worker-3'
```

## 4. GREEN 原始读数（脚本 stdout，逐字，本轮）

```json
{
  "lane": {
    "api_url": "http://127.0.0.1:3300",
    "internal_url": "http://control-plane:3000",
    "worker_exec_template": "docker exec -i n83narrow-{node}-1 bash -lc",
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
      "measured_cpu_percent": 99.96044770264452,
      "cpu_max_readback": "100000 100000",
      "sandbox_cgroup": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd/sbx_sbx_cd0674e43188891f",
      "spinner_node": "worker-3",
      "first_sandbox_rtt_quiet": {
        "samples_ms": [
          368.52,
          31.74,
          29.35,
          32.39,
          35.72
        ],
        "min_ms": 29.35,
        "median_ms": 32.39
      },
      "second_sandbox_rtt": {
        "samples_ms": [
          150.4,
          45.55,
          31.61,
          34.48,
          41.39
        ],
        "min_ms": 31.61,
        "median_ms": 41.39
      },
      "round_trip_criterion": "min-of-5 neighbour <= 3x the quiet min-of-5 (floor 50 ms) -- detects gross starvation (the N82 shape stalled 860 ms); a subtle slowdown is below its resolution and is caught by check 3's cgroup accounting instead",
      "round_trip_bound_ms": 88.05,
      "second_sandbox_node": "worker-3",
      "second_sandbox_same_node": true
    },
    "2_kernel_enforces": {
      "pass": true,
      "cgroup": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd/sbx_sbx_cd0674e43188891f",
      "cpu_max": "100000 100000",
      "window_s": 3.126,
      "usage_usec_delta": 3101766,
      "nr_throttled_delta": 31,
      "throttled_usec_delta": 9301890,
      "observed_cores": 0.992,
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
        "probe_output": "DONE op=openclose stalls=0 rounds=198 elapsed_s=40.0 ops_per_s=9895",
        "ops_per_s": 9895,
        "elapsed_s": 43.1,
        "sandbox_id": "sbx_e63480fa3fec7180",
        "node_id": "worker-3",
        "cgroup": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd/sbx_sbx_e63480fa3fec7180",
        "cgroup_samples": [
          {
            "at_s": 2.73,
            "path": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd/sbx_sbx_e63480fa3fec7180",
            "cpu_stat": {
              "usage_usec": 1679189,
              "user_usec": 535768,
              "system_usec": 1143420,
              "nice_usec": 0,
              "nr_periods": 21,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 4.86,
            "path": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd/sbx_sbx_e63480fa3fec7180",
            "cpu_stat": {
              "usage_usec": 3441164,
              "user_usec": 1081481,
              "system_usec": 2359682,
              "nice_usec": 0,
              "nr_periods": 42,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 6.98,
            "path": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd/sbx_sbx_e63480fa3fec7180",
            "cpu_stat": {
              "usage_usec": 5202143,
              "user_usec": 1606723,
              "system_usec": 3595420,
              "nice_usec": 0,
              "nr_periods": 63,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 9.1,
            "path": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd/sbx_sbx_e63480fa3fec7180",
            "cpu_stat": {
              "usage_usec": 6967829,
              "user_usec": 2162073,
              "system_usec": 4805755,
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
            "at_s": 11.23,
            "path": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd/sbx_sbx_e63480fa3fec7180",
            "cpu_stat": {
              "usage_usec": 8730547,
              "user_usec": 2711449,
              "system_usec": 6019098,
              "nice_usec": 0,
              "nr_periods": 106,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 13.35,
            "path": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd/sbx_sbx_e63480fa3fec7180",
            "cpu_stat": {
              "usage_usec": 10468270,
              "user_usec": 3235681,
              "system_usec": 7232589,
              "nice_usec": 0,
              "nr_periods": 127,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 15.47,
            "path": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd/sbx_sbx_e63480fa3fec7180",
            "cpu_stat": {
              "usage_usec": 12205945,
              "user_usec": 3737504,
              "system_usec": 8468440,
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
            "at_s": 17.59,
            "path": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd/sbx_sbx_e63480fa3fec7180",
            "cpu_stat": {
              "usage_usec": 13940449,
              "user_usec": 4237150,
              "system_usec": 9703299,
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
            "at_s": 19.71,
            "path": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd/sbx_sbx_e63480fa3fec7180",
            "cpu_stat": {
              "usage_usec": 15683518,
              "user_usec": 4740248,
              "system_usec": 10943270,
              "nice_usec": 0,
              "nr_periods": 191,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 21.84,
            "path": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd/sbx_sbx_e63480fa3fec7180",
            "cpu_stat": {
              "usage_usec": 17442829,
              "user_usec": 5272892,
              "system_usec": 12169937,
              "nice_usec": 0,
              "nr_periods": 212,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 23.96,
            "path": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd/sbx_sbx_e63480fa3fec7180",
            "cpu_stat": {
              "usage_usec": 19181278,
              "user_usec": 5813130,
              "system_usec": 13368147,
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
            "at_s": 26.08,
            "path": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd/sbx_sbx_e63480fa3fec7180",
            "cpu_stat": {
              "usage_usec": 20918695,
              "user_usec": 6340242,
              "system_usec": 14578453,
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
            "at_s": 28.2,
            "path": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd/sbx_sbx_e63480fa3fec7180",
            "cpu_stat": {
              "usage_usec": 22640332,
              "user_usec": 6812226,
              "system_usec": 15828106,
              "nice_usec": 0,
              "nr_periods": 276,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 30.32,
            "path": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd/sbx_sbx_e63480fa3fec7180",
            "cpu_stat": {
              "usage_usec": 24390613,
              "user_usec": 7393667,
              "system_usec": 16996946,
              "nice_usec": 0,
              "nr_periods": 297,
              "nr_throttled": 0,
              "throttled_usec": 0,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 32.44,
            "path": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd/sbx_sbx_e63480fa3fec7180",
            "cpu_stat": {
              "usage_usec": 26142470,
              "user_usec": 7895007,
              "system_usec": 18247463,
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
            "at_s": 34.57,
            "path": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd/sbx_sbx_e63480fa3fec7180",
            "cpu_stat": {
              "usage_usec": 27899675,
              "user_usec": 8448624,
              "system_usec": 19451050,
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
            "at_s": 36.69,
            "path": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd/sbx_sbx_e63480fa3fec7180",
            "cpu_stat": {
              "usage_usec": 29657946,
              "user_usec": 8986861,
              "system_usec": 20671085,
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
            "at_s": 38.81,
            "path": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd/sbx_sbx_e63480fa3fec7180",
            "cpu_stat": {
              "usage_usec": 31397044,
              "user_usec": 9478282,
              "system_usec": 21918761,
              "nice_usec": 0,
              "nr_periods": 382,
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
          0.831,
          0.833,
          0.828,
          0.82,
          0.82,
          0.818,
          0.822,
          0.826,
          0.82,
          0.82,
          0.812,
          0.826,
          0.826,
          0.825,
          0.829,
          0.82
        ],
        "peak_cores": 0.833,
        "nr_throttled_delta": 0
      },
      "flood_under_own_spinners": {
        "label": "4 concurrent copies of the probe's openclose program, one sandbox",
        "harness": "probe_n82_traced_syscall_costs.INNER imported verbatim, run in a sandbox we own",
        "sandbox_id": "sbx_2f8f188eab73685c",
        "node_id": "worker-3",
        "cgroup": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd/sbx_sbx_2f8f188eab73685c",
        "ops_per_s": 12068,
        "per_client_ops_per_s": [
          3018,
          3017,
          3016,
          3017
        ],
        "output": "DONE op=openclose stalls=0 rounds=31 elapsed_s=20.5 ops_per_s=3018\nDONE op=openclose stalls=0 rounds=31 elapsed_s=20.6 ops_per_s=3017\nDONE op=openclose stalls=0 rounds=31 elapsed_s=20.6 ops_per_s=3016\nDONE op=openclose stalls=0 rounds=31 elapsed_s=20.5 ops_per_s=3017",
        "cgroup_samples": [
          {
            "at_s": 2.01,
            "path": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd/sbx_sbx_2f8f188eab73685c",
            "cpu_stat": {
              "usage_usec": 2095856,
              "user_usec": 817713,
              "system_usec": 1278142,
              "nice_usec": 0,
              "nr_periods": 26,
              "nr_throttled": 1,
              "throttled_usec": 11948,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 4.14,
            "path": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd/sbx_sbx_2f8f188eab73685c",
            "cpu_stat": {
              "usage_usec": 4192810,
              "user_usec": 1597763,
              "system_usec": 2595046,
              "nice_usec": 0,
              "nr_periods": 47,
              "nr_throttled": 5,
              "throttled_usec": 16855,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 6.28,
            "path": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd/sbx_sbx_2f8f188eab73685c",
            "cpu_stat": {
              "usage_usec": 6291694,
              "user_usec": 2495506,
              "system_usec": 3796187,
              "nice_usec": 0,
              "nr_periods": 68,
              "nr_throttled": 7,
              "throttled_usec": 17682,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 8.42,
            "path": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd/sbx_sbx_2f8f188eab73685c",
            "cpu_stat": {
              "usage_usec": 8371930,
              "user_usec": 3319632,
              "system_usec": 5052298,
              "nice_usec": 0,
              "nr_periods": 90,
              "nr_throttled": 7,
              "throttled_usec": 17682,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 10.54,
            "path": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd/sbx_sbx_2f8f188eab73685c",
            "cpu_stat": {
              "usage_usec": 10452212,
              "user_usec": 4159959,
              "system_usec": 6292253,
              "nice_usec": 0,
              "nr_periods": 111,
              "nr_throttled": 7,
              "throttled_usec": 17682,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 12.66,
            "path": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd/sbx_sbx_2f8f188eab73685c",
            "cpu_stat": {
              "usage_usec": 12549193,
              "user_usec": 5018635,
              "system_usec": 7530558,
              "nice_usec": 0,
              "nr_periods": 132,
              "nr_throttled": 8,
              "throttled_usec": 18939,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 14.8,
            "path": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd/sbx_sbx_2f8f188eab73685c",
            "cpu_stat": {
              "usage_usec": 14652351,
              "user_usec": 5904678,
              "system_usec": 8747672,
              "nice_usec": 0,
              "nr_periods": 154,
              "nr_throttled": 9,
              "throttled_usec": 19601,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 16.94,
            "path": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd/sbx_sbx_2f8f188eab73685c",
            "cpu_stat": {
              "usage_usec": 16743791,
              "user_usec": 6748056,
              "system_usec": 9995734,
              "nice_usec": 0,
              "nr_periods": 175,
              "nr_throttled": 12,
              "throttled_usec": 21636,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 19.06,
            "path": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd/sbx_sbx_2f8f188eab73685c",
            "cpu_stat": {
              "usage_usec": 18835676,
              "user_usec": 7628006,
              "system_usec": 11207670,
              "nice_usec": 0,
              "nr_periods": 196,
              "nr_throttled": 13,
              "throttled_usec": 22638,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          },
          {
            "at_s": 21.19,
            "path": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd/sbx_sbx_2f8f188eab73685c",
            "cpu_stat": {
              "usage_usec": 20284969,
              "user_usec": 8258768,
              "system_usec": 12026200,
              "nice_usec": 0,
              "nr_periods": 215,
              "nr_throttled": 14,
              "throttled_usec": 23799,
              "nr_bursts": 0,
              "burst_usec": 0
            },
            "cpu_max": "100000 100000"
          }
        ],
        "interval_cores": [
          0.984,
          0.981,
          0.972,
          0.981,
          0.989,
          0.983,
          0.977,
          0.987,
          0.68
        ],
        "peak_cores": 0.989,
        "nr_throttled_delta": 13
      }
    },
    "4_narrowing_view_shape": {
      "pass": true,
      "criterion": "own delegated cgroup writable (cgroup.procs/subtree_control, NOT cpu.max); every visible peer container's cpu.max still EACCES; every non-delegated peer's cpu.max/cgroup.procs/mkdir EACCES",
      "workers": {
        "worker-1": {
          "mount": "/pod-cgroup",
          "ls_mount": [
            "ac66ff04ae860124c141a1f204f8440d4b38f6e319181f4b2fb66f4986f0c85a",
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
          "ls_mount_count": 50,
          "proc_self_cgroup": "0::/worker",
          "hostname": "ac66ff04ae86",
          "own": {
            "path": "/pod-cgroup/ac66ff04ae860124c141a1f204f8440d4b38f6e319181f4b2fb66f4986f0c85a",
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
          "peer_containers": [],
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
            "own": "/pod-cgroup/ac66ff04ae860124c141a1f204f8440d4b38f6e319181f4b2fb66f4986f0c85a",
            "children": [
              "worker"
            ],
            "proc_self_cgroup": "0::/worker"
          },
          "own_delegated": true,
          "peer_containers_count": 0,
          "foreign_peers": [],
          "foreign_peers_closed": false,
          "every_peer_cpu_max_closed": true,
          "delegated_peers": [],
          "peer_visible": false,
          "mount_looks_narrowed": true,
          "check4_mode": "narrowed-mount",
          "mount_root_closed": true,
          "evidence": true,
          "cpu_max_closed": true
        },
        "worker-2": {
          "mount": "/pod-cgroup",
          "ls_mount": [
            "057a28af3ba953e4c4478cfe5105afcac104b2368cb82ce04b1434600dd18c0a",
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
          "ls_mount_count": 50,
          "proc_self_cgroup": "0::/worker",
          "hostname": "057a28af3ba9",
          "own": {
            "path": "/pod-cgroup/057a28af3ba953e4c4478cfe5105afcac104b2368cb82ce04b1434600dd18c0a",
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
          "peer_containers": [],
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
            "own": "/pod-cgroup/057a28af3ba953e4c4478cfe5105afcac104b2368cb82ce04b1434600dd18c0a",
            "children": [
              "worker"
            ],
            "proc_self_cgroup": "0::/worker"
          },
          "own_delegated": true,
          "peer_containers_count": 0,
          "foreign_peers": [],
          "foreign_peers_closed": false,
          "every_peer_cpu_max_closed": true,
          "delegated_peers": [],
          "peer_visible": false,
          "mount_looks_narrowed": true,
          "check4_mode": "narrowed-mount",
          "mount_root_closed": true,
          "evidence": true,
          "cpu_max_closed": true
        },
        "worker-3": {
          "mount": "/pod-cgroup",
          "ls_mount": [
            "9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd",
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
          "ls_mount_count": 50,
          "proc_self_cgroup": "0::/worker",
          "hostname": "9563794dd95d",
          "own": {
            "path": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd",
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
          "peer_containers": [],
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
            "own": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd",
            "children": [
              "sbx_sbx_2f8f188eab73685c",
              "worker"
            ],
            "proc_self_cgroup": "0::/worker"
          },
          "own_delegated": true,
          "peer_containers_count": 0,
          "foreign_peers": [],
          "foreign_peers_closed": false,
          "every_peer_cpu_max_closed": true,
          "delegated_peers": [],
          "peer_visible": false,
          "mount_looks_narrowed": true,
          "check4_mode": "narrowed-mount",
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
      "sandbox_id": "sbx_cd0674e43188891f",
      "node_id": "worker-3",
      "cgroup": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd/sbx_sbx_cd0674e43188891f"
    },
    {
      "sandbox_id": "sbx_d2af82f4c49dc53f",
      "node_id": "worker-2",
      "cgroup": "/pod-cgroup/057a28af3ba953e4c4478cfe5105afcac104b2368cb82ce04b1434600dd18c0a/sbx_sbx_d2af82f4c49dc53f"
    },
    {
      "sandbox_id": "sbx_c73ad100c572826d",
      "node_id": "worker-1",
      "cgroup": "/pod-cgroup/ac66ff04ae860124c141a1f204f8440d4b38f6e319181f4b2fb66f4986f0c85a/sbx_sbx_c73ad100c572826d"
    },
    {
      "sandbox_id": "sbx_6c8a56a67cc11dd2",
      "node_id": "worker-3",
      "cgroup": "/pod-cgroup/9563794dd95d10a28b763964591d30cfccc49cb51b5a7dce62382792e439f4dd/sbx_sbx_6c8a56a67cc11dd2"
    }
  ],
  "started_at": "2026-10-06T18:31:23+0800",
  "elapsed_s": 103.3,
  "ok": true
}
```

## 5. RED 原始读数（`E2B_SANDBOX_CGROUP=off`，本轮，逐字）

```json
{
  "lane": {
    "api_url": "http://127.0.0.1:3300",
    "internal_url": "http://control-plane:3000",
    "worker_exec_template": "docker exec -i n83narrow-{node}-1 bash -lc",
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
      "measured_cpu_percent": 399.9338450189414,
      "cpu_max_readback": null,
      "sandbox_cgroup": null,
      "spinner_node": "worker-3",
      "first_sandbox_rtt_quiet": {
        "samples_ms": [
          264.88,
          35.17,
          29.93,
          31.66,
          31.04
        ],
        "min_ms": 29.93,
        "median_ms": 31.66
      },
      "second_sandbox_rtt": {
        "samples_ms": [
          185.57,
          49.69,
          29.08,
          27.52,
          43.76
        ],
        "min_ms": 27.52,
        "median_ms": 43.76
      },
      "round_trip_criterion": "min-of-5 neighbour <= 3x the quiet min-of-5 (floor 50 ms) -- detects gross starvation (the N82 shape stalled 860 ms); a subtle slowdown is below its resolution and is caught by check 3's cgroup accounting instead",
      "round_trip_bound_ms": 89.79,
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
        "probe_output": "DONE op=openclose stalls=0 rounds=183 elapsed_s=40.1 ops_per_s=9125",
        "ops_per_s": 9125,
        "elapsed_s": 42.6,
        "sandbox_id": "sbx_637a70f4cc0806b1",
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
      "criterion": "own delegated cgroup writable (cgroup.procs/subtree_control, NOT cpu.max); every visible peer container's cpu.max still EACCES; every non-delegated peer's cpu.max/cgroup.procs/mkdir EACCES",
      "workers": {
        "worker-1": {
          "error": "worker command on worker-1 exited 1: cannot pick this worker's delegated container cgroup under /pod-cgroup: 0 candidates, 0 matching hostname a3250ffc4c16: []"
        },
        "worker-2": {
          "error": "worker command on worker-2 exited 1: cannot pick this worker's delegated container cgroup under /pod-cgroup: 0 candidates, 0 matching hostname e3163e5e9db0: []"
        },
        "worker-3": {
          "error": "worker command on worker-3 exited 1: cannot pick this worker's delegated container cgroup under /pod-cgroup: 0 candidates, 0 matching hostname a08bb7e77c6a: []"
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
      "sandbox_id": "sbx_72030e41d89d1645",
      "node_id": "worker-3",
      "cgroup": null
    },
    {
      "sandbox_id": "sbx_fec7aa8136007e79",
      "node_id": "worker-2",
      "cgroup": null
    },
    {
      "sandbox_id": "sbx_d0bb1a4a46c6c0df",
      "node_id": "worker-1",
      "cgroup": null
    },
    {
      "sandbox_id": "sbx_c0a826b8036ce6f6",
      "node_id": "worker-3",
      "cgroup": null
    }
  ],
  "started_at": "2026-10-06T18:33:35+0800",
  "elapsed_s": 74.8,
  "ok": false
}
```

## 6. RED 的补充读数：这笔 CPU 记在**谁**头上（本轮）

同一条 `openclose` 洪泛（限流关、**没有**每沙箱 cgroup）期间，每 2 s 采三台 worker **自己容器
cgroup** 的 `cpu.stat`（一次性探针 `tmp/n83narrow-red-neighbour-cpu.py`，未入库；相对 Task 7 那支只改了
车道名与"自家 cgroup 现在直接挂在挂载根下"这条路径规则）：

```
worker-1: peak 0.032 cores, median 0.028 cores
worker-2: peak 0.865 cores, median 0.857 cores
worker-3: peak 0.034 cores, median 0.027 cores
```

即：洪泛那 ~0.87 核**记在 hosting worker（本轮是 worker-2）的容器 cgroup 上**，租户账上是 0 —— 与 N82
（`docs/open-issues.md` N82：18149 op/s、1.02 核记在 worker pod）同形。GREEN 档里同一笔 CPU 落在
`sbx_<id>` 子树里（单跑 0.833 核、四路并发 0.989 核）。

探针自己的输出是 `DONE op=openclose stalls=0 rounds=188 elapsed_s=40.1 ops_per_s=9367`；完整 JSON（含
每个采样点的 `usage_usec`）见工作树 `tmp/n83narrow-red-neighbour-cpu.json`。

## 7. 五条检查逐条判定

| # | 判据（脚本里的实际条件） | GREEN 读数（本轮） | 判定 |
|---|---|---|---|
| ① 额度是真的 | `measuredCpuPercent ∈ [50,150]`（声明 100）且同节点第二箱往返 min-of-5 ≤ 3× 静默 min-of-5（下限 50 ms） | `99.96`；`cpu_max=100000 100000`；第二箱 **31.61 ms** vs 静默 **29.35 ms**（上限 88.05 ms）；hosting 节点 worker-3 | PASS |
| ② 内核在强制 | 3 s 自旋窗口内 `nr_throttled_delta > 0` 且 `observed_cores ∈ [0.5,1.5]×quota` | `usage_usec +3,101,766`、`nr_throttled +31`、`throttled_usec +9,301,890`、`0.992 核` | PASS |
| ③ 洪泛花自己的额度 | 探针单跑 `peak ≤ 1.15×quota` **且** 四路并发 `peak ≤ 1.15×quota` **且** `nr_throttled > 0` **且**两次都真拿到 `ops_per_s` | 单独 `9,895 op/s / 0.833 核`；四路 `12,068 op/s（3018×4）/ 0.989 核 / nr_throttled +13` | PASS |
| ④ 收窄/视图形状 | 自家被委派的 cgroup 可写（`cgroup.procs`/`subtree_control`，**不含** `cpu.max`）；每个可见 peer 的 `cpu.max` 仍 EACCES；非被委派 peer 的 `cpu.max`/`cgroup.procs`/`mkdir` 全 EACCES；**没有 peer 可见时（收窄挂载）挂载根三条写全不可写** | 三台一致：`check4_mode = "narrowed-mount"`、`peer_containers_count = 0`、`mount_looks_narrowed = true`、`mount_root_closed = true`、`evidence = true`；自家容器目录属主 65534、`subtree_control=cpu`、自家 `cpu.max` EACCES | PASS |
| ⑤ 负例 fail-closed | 三台 worker 自家 `cpu.max` 的 `open(O_WRONLY)` == `EACCES` | worker-1/2/3 全 `EACCES` | PASS |

RED 档同一条脚本的判定（**这就是"不弱化"的证据**：RED 五条全 FAIL，且每条都给了具名理由）：

| # | RED 读数（本轮） | 判定 |
|---|---|---|
| ① | `measuredCpuPercent = 399.93`、`sandbox_cgroup = null`（邻居 min 27.52 ms vs 静默 29.93 ms —— 见 §8 的说明：这条 rtt 判据只能抓粗粒度饿死） | FAIL |
| ② | `reason: there is no sbx_<id> cgroup … (E2B_SANDBOX_CGROUP off?)` | FAIL |
| ③ | 探针 `9,125 op/s`，`peak_cores = null`；binding harness 具名拒绝（找不到 `sbx_<id>`） | FAIL |
| ④ | `cannot pick this worker's delegated container cgroup under /pod-cgroup: 0 candidates`（`off` 档没有委派 ⇒ 没有 `worker/` 子树可认） | FAIL |
| ⑤ | 没有被委派的容器目录 ⇒ 三台都判不出"自家 cgroup" | FAIL |

## 8. 没做 / 没验证的（如实说）

* **线上（k0s）没滚**：Task 7 的 Step 3（两次 apply）与线上复验（k8s 版 ④ + 端到端冒烟）**没做**，
  本轮范围同样止于"本地 lane 全绿 + 文档 + 归档"。上线剧本与回退杆写在 `docs/deploy-clusters.md` §7.48。
* **本轮的收窄只在 compose 车道上验过**：k8s 的挂载形状没变（`subPathExpr` 那条才是 k8s 的收窄装置），
  但"compose 用静态父切片收窄"这件事**只在本地 Docker VM 上量过** —— k0s 上 compose 不跑。
  `deploy/compose/docker-compose.prod.yml` 与 `deploy/stack/docker-compose.prod.yml` 是**同形的两份**，
  本轮**没有起它们**（只跑 `docker compose config` 校验插值与 bind 源一致性），CI 钉子覆盖它们的文本形状。
* **内存/进程数**（`memory.max` / `pids.max`）是 Phase 2，本任务不涉及（计划 D6）。
* **邻居保护**：只量了"同节点第二箱的往返不掉速"这一条；节点级公平（CFS 层次带宽）没有单独测量，而且这条
  rtt 判据**只能发现粗粒度饿死** —— RED 档（4 自旋、无 cgroup）量到邻居 27.52 ms vs 静默 29.93 ms，
  **确实没掉速**，所以它在这条车道上证明不了"邻居没被吵"；那件事的正面证据是 ③ 的额度记账。
* **`E2B_SANDBOX_NOTIFY_RATE_LIMIT=0` 只在这次验收的栈里**：生产清单没改，N83 Phase 1 的 Task 8
  （限流器降级）还没有做。
* 本机 Docker VM 是 **cgroup v2 / x86_64 / cgroupfs driver**（`docker info`：`CgroupVersion 2`、
  `Architecture x86_64`、6 核 / 20 GB）。k0s 节点是 arm64 的 4 核，那边的形状仍需按 §7.48 的剧本复验。
* **RED 档仍会留下沙箱目录/记录**：`off` 档没有 cgroup 可回收，验收里建的沙箱由脚本 `kill()` 掉；
  拆栈时 `down -v` 把卷一起删掉，所以没有残留（§9.2 的核对）。

## 9. 收尾

### 9.1 单元 / 钉子测试（本机 macOS）

任务点名的三条文件，`/Users/polus/project/ai/sandlock-e2b/.venv/bin/python -m pytest` ⇒
**`2 failed, 73 passed`**：

* 红的两条在 `tests/unit/test_docs_only_point_at_repo_artifacts.py`：
  `test_every_live_doc_tmp_reference_is_allowed_with_a_reason`（`tmp/clone3_probe.py`、
  `tmp/userns_thread_probe.py`）与 `test_no_live_doc_names_a_script_that_this_repo_does_not_have`
  （`identity_grant.py`）。**这两条是本轮之前就存在的**（它们读的文档与这两个 tmp 脚本都不是本轮改动；
  本轮 `git diff --name-only` 里没有那个测试文件），与 compose 收窄无关，按任务要求**原样留下**。
* `tests/unit/test_worker_manifest_permissions.py`（含本轮新增的
  `test_compose_worker_cgroup_bind_is_exactly_its_own_parent`）与
  `tests/unit/test_compose_base_image_shape.py` **全绿**。

### 9.2 拆栈与"没有残留"的核对

```text
docker compose -p n83narrow -f … -f tmp/n83narrow-override-off.yml down -v
docker ps -a --filter name=n83narrow --format '{{.Names}}' | wc -l      ⇒ 0
docker volume ls --filter name=n83narrow --format '{{.Name}}'           ⇒ (空)
docker network ls --filter name=n83narrow --format '{{.Name}}'          ⇒ (空)
ls /sys/fs/cgroup | grep -c n83narrow                                   ⇒ 0（三个父切片已手工 rmdir）
用户 live 栈：docker ps --filter name=compose … ⇒ 7 个容器仍 Up（未被触碰）
```

`tmp/` 下（gitignored，不进仓库）：两份 override、两份验收原始 JSON、RED 邻居探针的 JSON、构建日志。

## 10. 与计划的偏差（都要有人知道）

1. **执行者与立项文本不同**：计划 §3.2 定的是"worker 自管、agent 只做一次性委派"（形态 W），N83 行里
   那段"由 agent 建子 cgroup"是更早的 D2/D3 版本；实现与验收都按 §3.2（§7.48 同口径）。
2. **check ③ 有第二条读数**：计划只要求"洪泛仍落在额度内"。单独跑是 `0.833 核 ≤ 1 核`，但那条**没有**
   证明额度 binding（负载自限在 1 核以下）。所以同一条检查里追加"四路并发"读数（用探针自己的 INNER
   程序，import 而非抄写）—— `0.989 核（3018×4）` + `nr_throttled +13` 才真正说明"沙箱自己的额度在
   bound 它"。判据里两条都要过。
3. **check ④ 的本地判据本轮换成了 `narrowed-mount`**：计划当初的写法是"本地 lane = 整棵树、peer 逐个探"
   （因为那时 compose 没收窄）；compose 收窄之后，本地 ④ 与 k8s 同形 —— **没有 peer 可见**，
   证据是"挂载看起来是收窄的 **且** 挂载根三条写全不可写"。脚本的两种形状（`peer-container` /
   `narrowed-mount`）都在 JSON 里（`check4_mode`），判决条件不变、没有放宽。
4. **§5 第四条风险（同 uid 的 peer 可写）已按今天的形状 re-scope**：原读数保留为历史
   （`docs/deploy-clusters.md` §7.48 坑 4 与计划 §5），今天的 compose 与 k8s 两条车道都不再有这个形状。

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
