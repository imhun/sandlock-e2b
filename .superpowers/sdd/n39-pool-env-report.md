# 债务报告：N39 收口（本地池的 worker env）

日期：2026-09-27 ｜ 工作目录：`/Users/polus/project/ai/sandlock-e2b`
HEAD（开工时）：`1549255`（执行期间并行工作流又落了 `59c3d4e` / `e31ee4f` / `84e21c8` / `a3d0957`）
登记：`docs/open-issues.md` 的 **N39**（收口）与 **N45**（新登记）

---

## 0. 结论（先给结论）

1. **N39-① 已清（实测，不是看账）**：`E2B_AS_WORKER_ENV` 现在逐键列出了车队那几项，池 spawn 出来的 worker **起得来**——`Uvicorn running on …:49983` + `registered node …`，容器 `state=running / exitcode=0`，SDK 通过池真的建出了沙箱（`POOL NETNS SHAPE OK`）。缺口是在 `fd81f26`（compose 一处）与 `d87834b`（autoscaler 自带字典）补上的，早于本单。
2. **N39-② 仍成立，但它是环境纪事、不是代码缺陷**：池**默认** worker tag 还是 `registry.cn-shanghai.aliyuncs.com/byteplan/e2b-sandlock-worker:0.1.0`（`docker image inspect` ⇒ `created=2026-08-30T08:48:57Z`）。该镜像里 `/app/envd_service/config.py` **0 次**出现 `E2B_ENABLE_NET_ISOLATION` / `E2B_ROUTE_B_TMP_ROOT`（⇒ 零 netns 代码、`auto` 空转），而且本机实测它**连车队那条 digest 基镜像都解析不了**（`manifests/... -> 404`，`Sandbox.create` 恒 `428 warm_required`）。**要在池上做形态/出网/MCP 验证，必须显式传 `WORKER_IMAGE=<本树构建的 tag>`；这是运维纪事（默认 tag 要么显式指、要么有意留旧）。**
3. **发现一条真缺陷，已按新登记写进 N45**：池的 worker 形态缺 `E2B_PID_NS`（车队两份清单都开），实测池里的沙箱与 worker **共 pid 命名空间**——沙箱内 `kill(1,0)=EPERM`（看得见 worker 的 pid 1）、`getpid=81`；把 `E2B_PID_NS=true` 追加进池 worker env 后同一探针变成 `getpid=7 / kill(1,0)=ok / kill(2,0)=ok`（判据可翻转，非恒真）。

---

## 1. 静态核实：`E2B_AS_WORKER_ENV` 逐键（现在有什么）

权威声明在 **[`deploy/compose/docker-compose.autoscale.yml:159`](../deploy/compose/docker-compose.autoscale.yml)**（compose 渲染给 autoscaler 的那串 JSON），
以及 autoscaler 自带的兜底字典 **[`autoscaler/backends/local.py:71-84`](../autoscaler/backends/local.py)**（`DockerPoolBackend` 手搓时也自足）。逐键：

| 键 | 池的值（出厂默认） | 出处 | 车队对照 |
|---|---|---|---|
| `E2B_EXECUTOR` | `${E2B_EXECUTOR:-auto}` | autoscale:159 | k8s/compose 都不直接声明（镜像默认 `auto`，`envd_service/config.py:170`） |
| `E2B_ENABLE_NET_ISOLATION` | `"true"` | autoscale:159 + `local.py:71` | `deploy/stack/docker-compose.prod.yml:209`、`deploy/k8s/worker.yaml:408` |
| `E2B_FD_INJECT_CONNECT` | `"true"` | autoscale:159 + `local.py:72` | prod:210、k8s（`E2B_FD_INJECT_CONNECT`） |
| `E2B_ENABLE_NETWORK` | `"true"` | autoscale:159 + `local.py:83` | prod:196、k8s:426 |
| `E2B_ROUTE_B_TMP_ROOT` | `/var/lib/e2b-sandboxes/.route-b` | autoscale:159 + `local.py:84` | prod:239 同值、k8s:387 = `…/state/.route-b`（N27 下沉，池随 compose 形态） |
| `E2B_BASE_IMAGE` | 车队那条 MCP-capable digest（`python-mcp:3.14@sha256:3675…`） | autoscale:159 | k8s 同 digest |
| `E2B_IMAGE_CACHE_DIR / _MAX_BYTES / _EVICT_MIN_AGE_S / _OWNER_UID` | `/var/lib/e2b-sandboxes/_images` / `4294967296` / `300` / `65534` | autoscale:159 | 同车队 |
| `E2B_NODE_MEMORY_MB / CPU_PERCENT / DISK_MB / PROCESSES` | `2048 / 200 / 4096 / 256` | autoscale:159 | 车队同量级 |
| **`E2B_PID_NS`** | **没有（`envd_service/config.py:246` 默认 `false`）** | — | **车队有**：prod:220、k8s:406 ⇒ 见 §4 / N45 |

实测（池按出厂默认 spawn 的 worker 容器 env，原始输出 `tmp/n39/n39-pool-worker-facts.log`）确认上表逐条落地：
`E2B_EXECUTOR=auto`、`E2B_FD_INJECT_CONNECT=true`、`E2B_ENABLE_NET_ISOLATION=true`、`E2B_ENABLE_NETWORK=true`、
`E2B_ROUTE_B_TMP_ROOT=/var/lib/e2b-sandboxes/.route-b`、`sysctls=null`、`user=65534:65534`、**无 `E2B_PID_NS`**。

---

## 2. 静态核实：池默认 worker 镜像

| 位置 | 内容 |
|---|---|
| `deploy/compose/docker-compose.autoscale.yml:131`（autoscaler 给 worker 的 `E2B_AS_DOCKER_IMAGE`） | `${WORKER_IMAGE:-registry.cn-shanghai.aliyuncs.com/byteplan/e2b-sandlock-worker:0.1.0}` |
| `deploy/compose/docker-compose.autoscale.yml:19`（`image-cache-init` 借同一镜像的 `stat`/`chown`） | 同一个 tag |
| `deploy/compose/.env.example:104` | `WORKER_IMAGE=…/e2b-sandlock-worker:0.1.0`（照文档 `cp` 后把默认戳死成同一个旧 tag） |

**它还是 08-30 那版**（两条独立证据）：

1. `docker image inspect …/e2b-sandlock-worker:0.1.0 --format '{{.Created}}'` ⇒ `2026-08-30T08:48:57.078805292Z`；
   容器 `user=`（空 ⇒ root，与 `docs/HANDOFF.md:561` 记的旧镜像没有 `USER` 一致）。
2. 该镜像内 `/app/envd_service/config.py` 里 `E2B_ENABLE_NET_ISOLATION` / `E2B_ROUTE_B_TMP_ROOT` 各 **0** 次，
   本树构建的镜像各 1–2 次（原始输出 `tmp/n39/n39-image-netns-code.log`）⇒ 旧镜像**没有 netns 代码**、缺 route-B 键也**不报错**（它根本不读）。

> 这解释了一个容易读错的点：N39-① 的 "exit 1" 是**当前树构建的** worker 才有的（它读 route-B 键、缺则起不来）；
> 旧默认 tag 不读那个键，所以旧镜像反而是"缺键也起得来、但形状全旧"。两种都指向同一句：**默认 tag 不能用来做形态验证**。

---

## 3. 动态实测（本机 Docker，原始输出在 `tmp/n39/`）

起栈方式照 `deploy/compose/.env.example` 的说明（`cp` 到 `.env` 后 `docker compose -f … up -d --build`），
但**只在本机 Docker 做、只碰本项目**：独立 project（`COMPOSE_PROJECT_NAME=n39`）、独立端口（3910/3911）、
卷用 tmp-only override 换成 `n39-*`（**没有** `down -v`，共享的 `sandbox-shared` 卷一次都没动过）。

### 3.1 池按出厂默认（worktree 构建的 worker 镜像）

| 观察 | 原始输出 |
|---|---|
| worker 起得来 | `tmp/n39/n39-pool-worker.log`：`Uvicorn running on http://0.0.0.0:49983` / `registered node e2b-worker-…`；容器 `state=running exitcode=0` |
| `E2B_EXECUTOR` | `auto` |
| `sysctls` | `null` |
| 配对守卫 | autoscaler 日志 `NET_ISOLATION_PAIRING_ERROR` **0** 次 |
| SDK 探针 | `tmp/n39/n39-pool-shape.log` ⇒ **`POOL NETNS SHAPE OK`**（`ifaces=lo`、`command -v ip` ⇒ `no-ip`、`1+1=2` 都对） |

### 3.2 反证/对照臂

**A. 池用默认 worker tag（`WORKER_IMAGE=…:0.1.0`）**（`tmp/n39/n39-pool-default-image-*.log`）：
worker 起得来（`Uvicorn running`），但**建不了沙箱**——`Sandbox.create` 恒 `428 warm_required`，worker 侧
`RegistryError: … /v2/byteplan/python-mcp:3.14/manifests/sha256:3675… -> 404: 404 page not found`（4 次）。
⇒ 旧 tag 连车队那条 digest 基镜像都解析不了；"必须显式指镜像"这句有实测撑腰。

**B. 池加 `E2B_PID_NS=true`**（`tmp/n39/n39-pool-pidns-on.log`）：见 §4。

---

## 4. 新登记：N45（池的 worker 形态缺 `E2B_PID_NS`）

**根因（静态）**：`E2B_AS_WORKER_ENV`（autoscale:159）与 `autoscaler/backends/local.py:52-84` 的自带字典都没声明 `E2B_PID_NS`，
而 `envd_service/config.py:246` 的默认是 **`false`**；车队两份清单都开（`deploy/stack/docker-compose.prod.yml:220`、`deploy/k8s/worker.yaml:406-407`）。

**RED（池按出厂默认，探针 `tmp/n39/n39-pool-pidns-probe2.py`，输出 `tmp/n39/n39-pool-pidns.log`）**：

```
getpid=81
kill1=EPERM      # worker 的 pid 1（python -m envd_service）可见、不可 signal ⇒ 存在性预言机
kill2=ESRCH
kill999999=ESRCH
```

**GREEN（同池 + 只在 worker env 追加 `E2B_PID_NS: "true"`，输出 `tmp/n39/n39-pool-pidns-on.log`）**：

```
getpid=7
kill1=ok
kill2=ok
kill999999=ESRCH
```

两条唯一变量就是那个键 ⇒ **判据可翻转、不是恒真**（两个 worker 容器都 `state=running`，spawn 时间都晚于各自的 autoscaler，见两份 facts 日志）。

**为什么算真缺陷**：`deploy/k8s/worker.yaml:395-397` 自己写了后果——"without it a sandbox sees the pod's pids and `kill(pid, 0)`
answers EPERM for a live one (existence oracle)"；N38 的裁定又是"池要按车队形态跑"。**残留风险**：同一 worker 上多沙箱时可互探（默认容量 4096/256 是
一 worker 一沙箱，本机没测跨沙箱那一档）。

**下一步**：`E2B_PID_NS: "true"` 补进两处（autoscale:159 的 JSON + `local.py:71-84` 的字典），并把该键加进
`tests/unit/test_autoscaler_local_backend_shape.py` 现有的 fleet-key 钉子（`:268`）。**非车队的那几个 compose 栈同样没声明它**（`deploy/compose/*.yml` 全 0 处），
是否一并切由人拍。

---

## 5. 残余 / 未做

* **N39-② 的默认 tag** 仍未动（本单按任务要求只做收口与登记，不动默认值）。
* **N38 行尾那半句**（"池 worker env 缺 `E2B_ENABLE_NETWORK` 与 `E2B_ROUTE_B_TMP_ROOT`"）已被本单实测推翻，N39 行里已注明，未去改 N38 行（避免与并行 lane 撞同一行）。
* **跨沙箱 pid 互探**没测（默认容量一 worker 一沙箱）；要测需把 `E2B_NODE_PROCESSES` 抬到能容两个。

---

## 6. 文件清单与提交

**改动的仓库文件**：只有 `docs/open-issues.md`（N39 行收口 + 新增 N45 行）与本报告。
**证据（`tmp/n39/`，均未提交）**：

| 文件 | 内容 |
|---|---|
| `n39-worker-build.log` | 本树构建 worker 镜像（`e2b-local/e2b-sandlock-worker:n39`） |
| `n39-image-netns-code.log` | 新旧镜像 `config.py` 的 netns 键计数 |
| `n39-compose-config.log` | `compose config` 渲染（project n39） |
| `n39-pool-up.log` / `n39-pool-asshipped-restart.log` | 起栈 |
| `n39-pool-worker-facts.log` | 出厂默认 worker 的容器事实 + env + ready 行 |
| `n39-pool-worker.log` | worker 原始日志 |
| `n39-pool-shape.log` | SDK 形态探针 ⇒ `POOL NETNS SHAPE OK` |
| `n39-pool-pidns.log`（出厂默认，RED） / `n39-pool-pidns-on.log`（加键后，GREEN，复现两次） | pid 形态对照 |
| `n39-pool-pidns-on-facts.log` / `n39-pool-pidns-on-worker.log` / `n39-pool-pidns-on-up2.log` | 对照臂的容器事实、worker 日志、起栈记录 |
| `n39-pool-pidns-probe2.py` | pid 形态探针 |
| `n39-pool-default-image-*.log` | 默认 tag 对照臂（428 + 404 原始输出） |
| `n39-teardown.log` / `n39-teardown2.log` | 收尾（无 `-v`，只删 `n39-*`） |
