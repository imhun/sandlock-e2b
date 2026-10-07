# N83 Phase 2 线上重滚与验收（2026-10-07）

Phase 2（每沙箱 `memory.high`/`memory.max`/`pids.max` + 上限归控制面 + 节点容量取容器内核 +
车队总量按 Σ 节点推导 + CPU 可超卖）的**线上重滚**记录。本地车道的读数见
`docs/deploy-clusters.md` §7.50.1；本文只记线上。

## 版本与镜像

| 项 | 值 |
|---|---|
| 版本 tag | `0.1.0-1117-g23adedf-20261007-160606` |
| 镜像 | `e2b-sandlock-{control-plane-gateway,worker,agent,quota-agent}` 四件同 tag（多架构，`amd64`+`arm64`） |
| 合并 | `main` 从 `0fe05b9` **fast-forward** 到 `23adedf`（23 个提交）；未 push 到 GitHub |
| 集群 | 自建 k0s，2 节点 arm64，namespace `sandlock` |

## 上线顺序与时间线

顺序 = **先控制面、再 worker**（R17：上限是控制面的策略，随 register/heartbeat 响应下发）。仓库自己的
`deploy/k8s-k0s/apply.sh` 把整套清单**一次 `kubectl apply`**打上去、**不等控制面 Deployment**，所以这一轮
**分两次 apply**（见 `docs/deploy-clusters.md` §7.50 的"顺序靠人，脚本不替你排"）：

| 时刻 | 动作 |
|---|---|
| 16:17:11 | apply 第一批（控制面 Deployment + redis + 两个 DaemonSet，**不含** worker StatefulSet） |
| 16:17:31 | `rollout status deploy/control-plane` 收敛（2/2）；`ds/e2b-c3-agent` 同时收敛 |
| 16:18:01 | apply 第二批（worker StatefulSet） |
| 16:18:09 | `statefulset rolling update complete 2 pods` |
| 16:18:51 | 再跑一次 `apply.sh`（幂等重放 + **base image 预热**：两台 worker `cached=true`，无 428 窗口） |

**混版本窗口**（新控制面 + 旧 worker）≈ **40 s**（16:17:31→16:18:09）：期间旧 worker 心跳 **200**（新控制面
接受旧 worker 的五键 `sandboxCeiling` 并忽略其中策略三项），**0 条孤儿回收**（`TTL sweep … 0 candidate(s)
reaped`）、无建箱失败、无告警。

## 关键读数（线上）

**① 上限从控制面下发、worker 采纳**（两台各一条）：

```
node agent: adopted the control plane's per-sandbox ceiling (cpuPercent=400 memoryMB=4096 processes=1024)
cgroup lane ready (attempt 1): cgroup ready parent=/pod-cgroup/<容器 cgroup id> worker_uid=65534
  drained=1 subtree_control=cpu memory pids
```

第二行同时回答了计划里"只有线上能证"的第 1 条：**k8s pod 层确实把 `memory`/`pids` 委派下来了** ——
`subtree_control` 里三个控制器一次写成功（写不上会具名拒绝、建箱全拒）。

**② 节点记录两份读数**（`GET /internal/nodes`，两台一致）：

| 节点 | 策略（控制面）`sandbox*Max` | 节点总量 | 物理（内核）`kernelCPUPercent/kernelMemoryMB` |
|---|---|---|---|
| e2b-worker-0 | 400 / 4096 / 1024 | 400 / 4096 / 1024 | **400 / 4096** |
| e2b-worker-1 | 400 / 4096 / 1024 | 400 / 4096 / 1024 | **400 / 4096** |

物理那对 = `cpu.max` 4 核 / `memory.max` 4 GiB ⇒ 计划里"只有线上能证"的第 2 条（**容量确实取自容器内核**）
成立；策略与物理相等 ⇒ **CPU 超卖告警不误报**（两台 `oversell` 计数 **0**，第 4 条）。

**③ 上限的 env 位置**：控制面容器 `/proc/1/environ` 里有 `E2B_MAX_SANDBOX_{CPU_PERCENT,MEMORY_MB,PROCESSES}`
= 400/4096/1024；**worker 容器里三件套一个都没有**（`required` 仍在，`E2B_SANDBOX_CGROUP=required`）。

## 线上验收（`deploy/scripts/acceptance/cgroup_acceptance.py`）

调用（入口用**租户边车**，见"坑 1"）：

```
--api-url http://172.18.78.49:3000 --api-key <key>
--internal-url http://control-plane.sandlock.svc.cluster.local:3000 --internal-key <fleet key>
--worker-exec-template 'kubectl -n sandlock exec {node} -- bash -lc'
--control-plane-exec-template 'kubectl -n sandlock exec {node} -c control-plane -- bash -lc'
--control-plane-node control-plane-576b9b8759-rrfbv --nodes e2b-worker-0,e2b-worker-1
```

**结果：`ok: true`，9/9 全过，240.7 s**（①–⑤ 是 Phase 1 的五条，全部仍然绿）。Phase 2 的四条读数：

| 检查 | 线上读数 |
|---|---|
| ⑥ 内存上限**真杀** | `memory.max == memory.high == 67108864`（逐字等于声明的 64 MiB）；分配者 `hog_exit=137`（SIGKILL）；箱仍能应答；`memory.events` `oom_kill=1` / **`oom_group_kill=0`**（D3：只杀分配者）；`memory.peak` 到 67108864；**同节点邻居**往返 quiet 66.17 ms → busy 65.13 ms（界 198.51 ms，不掉速） |
| ⑦ 任务预算撞墙 | `pids.max == 256`（= 该箱**自己记录**里的 `max_processes`，源 `/var/lib/e2b-sandboxes/state/_runtime/<id>/sandbox.json`）；fork 248 次后 **`errno=11`（EAGAIN）**、`pids.events.max=1`、`pids.current` 峰值 256；同节点邻居 quiet 66.6 ms → busy 67.4 ms（界 199.8 ms） |
| ⑧ 越界具名 400 | 上限读数取自**控制面容器**内；`cpuCount 16` ⇒ `400 cpuCount 16 exceeds this node's per-sandbox maximum (4)`；`memoryMB 16385` ⇒ `400 … maximum (4096)`；**贴着上限**的请求 `201`，随后 `DELETE 204` |
| ⑨ peak 与任务单位 | `memory.max == memory.high == 268435456`（逐字等于 256 MiB）；`pids.max=256`；分配 64 MiB 后 `memory.peak=73211904`；持有者**加 2 个线程** ⇒ `pids.current` **+2**，**fork 1 个进程** ⇒ **+1** ⇒ **线程与进程共用同一个任务预算**（Review Focus §3 线上成立） |

## 容量观察（车队 = Σ 健康节点）

一次容量探针：连建 **1 核 / 512 MB** 的箱 —— 前 8 个 `201`（两台节点各 4 个，正好把各自的 400% 填满），
第 9 个也 `201`：**控制面的 autoscaler 在压力下把 `e2b-worker` 从 2 扩到 3**（日志 `scaled e2b-worker to 3`、
`retired drained node e2b-worker-3`），第 9 个箱落在**新节点** `e2b-worker-2` 上 —— 即车队预算随
Σ 健康节点一起长大（旧的写死 400% 只放得下 4 个）。探针的 9 个箱随后全部 `DELETE 204` 清掉，
StatefulSet 收回 `2 desired / 2 current / 2 ready`。（观察：被缩掉的 `e2b-worker-2/3` 会作为
`unhealthy` 节点记录短暂留存，由健康扫描/TTL 收走。）

## 坑（本轮踩到的）

1. **验收入口要用租户边车，不是节点 NodePort**：`--api-url http://172.18.80.94:31907`（节点 NodePort）
   从本机**不可达**（超时），第一次跑因此在建箱处得到 502/超时；正确入口是 `http://172.18.78.49:3000`
   （AGENTS.md 记的那条 `…:3000 → .140:31907` 链路），用 `.apikey` 直接 `200`。
2. **构建别让它跟着会话走**：第一次把 `build-and-push.sh` 放后台、父会话一结束进程就被带走，日志停在
   `pushing layers`、四个镜像只推完 worker；重跑（同一 `VERSION`）才补齐 —— 复用同一个 tag 是关键，
   否则会推出两套不同 tag 的镜像。
3. **批量删箱别用无引号变量分词**：本机 shell 是 zsh，`for s in $ids` 不会按空格拆词（会把 9 个 id 当
   一个 URL），改成逐行读才删干净。
4. **`apply.sh` 不替你排顺序**（本文按"分两次 apply"做的）：一口气 apply 会让 worker 与控制面同时滚，
   先回来的 worker 拿不到下发 ⇒ 建箱全拒（fail-closed，但正是要避免的窗口）。

## 回退杆（与 §7.50 同一条）

把 `deploy/k8s-k0s/worker-capacity.patch.yaml:47-48` 的 `E2B_SANDBOX_CGROUP` 翻回 `"off"` 再 apply：
不再写 `memory.*`/`pids.max`、不建事件采样循环；内存退回 fork 的中介记账（超预算**杀分配者 + 答
`ENOMEM`**，账只覆盖载荷），任务数退回 clone 族计数（`EAGAIN`）；**请求侧的尺寸校验不受影响**
（上限是控制面的策略，R17 之后与这个开关无关）。上一版镜像 tag = `0.1.0-1089-g28fd5af-20261006-195538`。

## 第二次滚：follow-up 批（2026-10-07 17:18–17:23）

上线的第一批（follow-up）与第一次同形：版本 **`0.1.0-1121-g8080d33-20261007-171729`**（`main` = `8080d33`），
四件镜像同 tag；**同样分两次 apply**：17:18:39 滚控制面（含 agent DaemonSet 收敛）→ 17:19:14 滚 worker
（`rolling update complete 2 pods`）→ 幂等重放 + base image 预热（两台 `cached=true`）。混版本窗口 ≈ 35 s。

这一批与上一批的差别只在**拒绝的形状与探针的声明**（`attach()` 的 `EACCES` 拒绝从错名的 `attach-io`
改成点名真因的 `attach-stat`；手工构造的全 `None` 上限从裸 `TypeError` 改成具名 `ceiling-unbounded`；
两个探针建箱时显式声明额度），**不改任何"沙箱能吃什么"的语义**。

复验（同一支脚本、同一入口）：

- 两台 worker 各打 `adopted the control plane's per-sandbox ceiling (cpuPercent=400 memoryMB=4096
  processes=1024)` 与 `cgroup lane ready … subtree_control=cpu memory pids`；
- **线上验收 `ok: true`，9/9 全过，244.2 s**。四条 Phase 2 读数与第一次逐条同形：
  ⑥ `memory.max == memory.high == 67108864`、`hog_exit=137`、`oom_kill=1`、**`oom_group_kill=0`**、
  同节点邻居 68.35 → 72.13 ms；⑦ `pids.max=256`、fork 后 **`errno=11`（EAGAIN）**、`pids.events.max=1`、
  邻居 68.82 → 67.91 ms；⑧ `400 cpuCount 16 exceeds this node's per-sandbox maximum (4)` 与
  `400 memoryMB 16385 …(4096)`，贴着上限 `201`/`204`；⑨ `memory.max == memory.high == 268435456`、
  `pids.max=256`、`memory.peak=73211904`、**线程 +2 / 进程 +1 都记在 `pids.current` 上**；
- 集群：全部 pod Running、**重启数 0**、无残留沙箱。

**回退杆**：同上（翻 `worker-capacity.patch.yaml:47-48`）；两次可退的上一版分别是
`0.1.0-1117-g23adedf-20261007-160606`（第一次）与 `0.1.0-1089-g28fd5af-20261006-195538`（Phase 1）。
