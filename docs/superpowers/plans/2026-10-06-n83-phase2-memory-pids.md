# N83 Phase 2：内存 + 进程数（实施计划）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> （推荐）或 superpowers:executing-plans 逐任务实施本计划。步骤用 `- [ ]` 复选框跟踪。

**Goal:** 把「沙箱能烧多少内存、能起多少任务」从**中介记账**换成**内核强制** —— 每箱
`memory.high`/`memory.max`/`pids.max` 写在自己的 `sbx_<id>` 里，超了按内核语义收场（内存
**SIGKILL**、任务数 **EAGAIN**），顺带把通知表里的 mmap 族与 clone 族退掉。

**Architecture:** 复用 Phase 1 已经落地的整条链（委派 → worker 自管 `sbx_<id>` → 收尾
`cgroup.kill`+`rmdir`）：`setup()` 里那句 `+cpu` 扩成 `+cpu +memory +pids`，`attach()` 里除
`cpu.max` 外再写 `memory.high`/`memory.max`/`pids.max`。**每箱上限（＝单沙箱最大可配置值）从
worker 容器自己的 cgroup 读**（`cpu.max`/`memory.max`/`pids.max`，就是集群在节点上配的那两个数），
随心跳上报给控制面；控制面据此校验客户端请求。

**Tech Stack:** Python 3.14（worker / control-plane / c3-agent）、cgroup v2（节点内核 6.12）、
Rust（fork 的通知表）、k8s 1.36 / k0s、compose（本地车道）。

**Spec:** 本文件；上游依据 `docs/superpowers/plans/2026-10-06-n83-per-sandbox-cgroup.md`
（Phase 1，已上线 `0.1.0-1089-g28fd5af`）、`docs/open-issues.md` 的 N82/N83/N84 行、
`docs/deploy-clusters.md` §7.48/§7.49。

## 用户已拍的三条（2026-10-06）

| # | 决定 | 含义 |
|---|---|---|
| **P1** | **pids 与内存一起做** | 一个 Phase，不拆两次上线 |
| **P2** | **`memory.max` 接 SIGKILL 语义** | 超预算 = 内核 OOM 杀进程（不是今天记账回的 `ENOMEM`）；这条**写进对外文档**，并接受它是产品语义变化 |
| **P3** | **集群上配的 CPU/内存上限 = 单沙箱最大可配置值** | 单箱可配到节点给 worker 的那份（今天 k8s 上 = **4 核 / 4 GiB**），但**不能超过**；超了**具名拒绝**，不静默夹取 |

## 现场事实（2026-10-06 实测，都是本次做的）

### 上限从哪儿读（k8s 两台一致）

| 读数（**worker 容器** cgroup，即 `sbx_<id>` 的父） | 值 |
|---|---|
| `cpu.max` | `400000 100000`（4 核） |
| `memory.max` | `4294967296`（4 GiB） |
| `pids.max` | `max`（今天没设） |
| `cgroup.subtree_control` | `[cpu]`（Phase 1 写的） |
| `cgroup.controllers` | 含 `memory`、`pids`（pod 层已委派） |

⇒ **上限不用新加配置项**：读 worker 容器 cgroup 的这三个文件就是"集群配的那份"。

### 能不能加（本地一次性容器实测）

```
+memory 写在容器 cgroup 上（父里还有进程）→ errno 16 (EBUSY)   ← 与 +cpu 同一条规则
把进程腾空到 worker/ 之后再写              → ok，回读 "memory"
子 cgroup 随即拿到 memory.max / memory.high / memory.events / memory.peak
```

Phase 1 的 `setup()` **已经在做**这个腾空 + 使能（今天只写 `+cpu`）⇒ 扩成三个控制器不需要新的
时序、权限或清单改动。

### 加了之后是什么语义（本地一次性容器实测）

| 设置 | 读数 |
|---|---|
| 只压 `memory.high=64M`（max=max，进程分配 256 MiB） | **存活**（exit 0）、**被节流**：52.8 s 只到 80 MiB、`memory.events` 的 `high` 计数 **11129**、`oom_kill 0`；`memory.peak` 到 **77.6 MiB** ⇒ 软限可被短暂超过 |
| `memory.max=64M`，箱内 `oom.group=0` | 分配者 **SIGKILL**、停在 64 MiB（`oom_kill 1`）；**同箱旁观者存活** |
| `memory.max=64M`，箱内 `oom.group=1` | 分配者 + **同箱旁观者全死**（`oom_group_kill 1`）⇒ 整个箱一起死 |
| **生产形状**：父（容器）`oom.group=1` + 箱内 `oom.group=0` | 箱撞自己的 max ⇒ **只杀箱内那个分配者**，旁观者活、父层不连坐（`oom_group_kill 0`、父 `cgroup.procs` 空） |
| `pids.max=8`，一个进程开线程 | 第 8 条线程失败 ⇒ **线程与进程共用一个预算**，错误是 **EAGAIN**（`pids.events = max 1`） |

两条要点：**父层 k8s 的 `oom.group=1` 不会放大沙箱的 OOM**（容器整体被杀只剩"聚合摸到 4 GiB"
这一条路，归准入）；**`pids` 数的 `pids.current` 是任务数（含线程）**，不是"进程数"。

## 定案（本计划的决定）

| # | 决定 | 理由 |
|---|---|---|
| D1 | Phase 2 一次接**三个限额**：`cpu.max`（已有）、`memory.high`+`memory.max`、`pids.max` | P1；三者共用同一次 `attach` 与同一个 read-back 纪律 |
| D2 | `memory.high = 声明额度`、`memory.max = 声明额度`（同一根线） | 先走 reclaim（节流），撑不住才杀 —— 与今天 `ENOMEM` 的**可观察差别最小**，同时满足 P2 的"超了就死" |
| D3 | 箱内 **`memory.oom.group` 保持默认 `0`**（只杀分配者），并把 `memory.events` 的 `oom_kill` 上报成具名告警 | 最小爆炸半径；"整箱一起死"作为一行可配置备选留档（改 `1` 即可），但默认不这么做 |
| D4 | `pids.max = max_processes`（整箱语义，M4 D6），**语义与今天完全一致**（EAGAIN） | 今天的强制者也是 EAGAIN（fork 的 `proc_count` 注释原话）⇒ 只换"谁数"，用户可见行为不变 |
| D5 | **单箱上限 = 读 worker 容器 cgroup**，控制面按它**校验**客户端请求（超了具名 400），不静默夹取 | P3；上限是集群事实，不该在代码里复制一份 |
| D6 | 真的解析创建请求里的 `cpuCount` / `memoryMB`（默认取 settings），并把 `record.cpu_count` 与 `record.memory_mb` 变成**唯一真相** | 顺手结掉 **N84**（今天 `cpu_count` 恒 1、客户端字段被静默忽略） |
| D7 | 通知表的 mmap 族与 clone 族**只在 `E2B_SANDBOX_CGROUP=required` 时**退掉 | `off` 的部署仍靠中介记账（逐字节回到今天），不退表 |
| D8 | 虚拟化数字（`/proc/meminfo`、`sysinfo`）**继续报声明额度** | 沙箱内 `free` 看到的应是"我这一箱有多少"，不是节点的 |

## Global Constraints

- **不给 worker / agent 任何新权限**：Phase 1 的委派清单（目录 + `cgroup.procs` + `cgroup.subtree_control`）
  与"零 capability"全部不动；`+memory`/`+pids` 只是往那个**已经可写**的 `subtree_control` 里多写两个词。
- **嵌套且取小**：每箱 cgroup 仍在 worker 容器 cgroup 之下（k8s 收窄、compose 静态父切片），
  容器/pod 的限额照旧生效。
- **fail 方向 closed**：写不上限额 / 回读不一致 / 拿不到上限 ⇒ 具名拒绝，**绝不无额度放行**。
- **上限只读**：从内核读（`cpu.max`/`memory.max`/`pids.max`），不从 env 复制；读不到就拒绝。
- **`E2B_SANDBOX_CGROUP=off` 逐字节回到今天**（不退通知表、不写 memory/pids 限额）。
- 断言精确（`==`），禁止部分匹配；每个写入后回读比对。

## Review Focus

1. **上限读错**（把 `max` 当成一个数、把 `pids.max=max` 当 0、把 pod 层的值当容器的值）⇒ 必须
   解析成"无上限"或具名拒绝，不能当成 `0`/`1` 写进限额。
2. **层级取小的直觉被破坏**（例如把 `memory.max` 设得比容器层还大）⇒ 内核会拒绝或静默按小的来，
   两种都要被回读抓到。
3. **`pids` 数的是任务**：默认 256 对多线程程序可能不够 ⇒ 验收里要有"线程也算"的读数，
   文档要写明（并把今天"supervise 占 1"的口径更新成"平台自己的进程也占任务数"）。
4. **OOM 的可见性**：被杀之后沙箱记录仍是 `running` ⇒ 必须有具名事件/告警，不能让用户只看到
   "进程莫名其妙没了"。
5. **退通知表的副作用**：mmap 族退掉后，`max_memory` 的**虚拟化**数字、以及 argv-safety 相关的
   clone 语义都要重验（不能只测"更快了"）。

---

## Tasks

### Task 1：worker 读「每箱上限」并上报

**Files:**
- Modify: `envd_service/runtime/sandbox_cgroup.py`（`setup()` 顺手读三个上限并挂在实例上）
- Modify: `envd_service/agent.py`（心跳 payload 加 `sandboxCeiling`）
- Modify: `control_plane/registry/nodes.py`（节点记录加三个字段 + `to_dict`/`from_dict`）
- Modify: `control_plane/api/internal.py`（`apply_*_report` 落库；节点内部视图并列暴露）
- Test: `tests/unit/test_sandbox_cgroup.py`、`tests/unit/test_c3_internal_api_shape.py`

**Interfaces:**
- Produces: `SandboxCgroups.ceiling -> SandboxCeiling(cpu_percent: int | None, memory_mb: int | None, processes: int | None)`
  （`None` = 内核说 `max`＝无上限）；心跳字段 `sandboxCeiling`；节点记录
  `sandbox_cpu_percent_max` / `sandbox_memory_mb_max` / `sandbox_processes_max`。

- [ ] **Step 1: 写失败测试**：① `cpu.max = "400000 100000"` ⇒ `cpu_percent == 400`；
  ② `memory.max = "4294967296"` ⇒ `memory_mb == 4096`；③ 两个字面 `max` ⇒ 字段为 `None`
  （**不是 0**）；④ 文件缺失 ⇒ 具名拒绝；⑤ 心跳带上 `sandboxCeiling` 且节点记录回读一致
- [ ] **Step 2: 跑测试确认失败**：`pytest tests/unit/test_sandbox_cgroup.py tests/unit/test_c3_internal_api_shape.py -q` ⇒ FAIL
- [ ] **Step 3: 实现**
- [ ] **Step 4: 跑测试确认通过**
- [ ] **Step 5: 提交**

### Task 2：控制面校验单箱请求（含 N84 收口）

**Files:**
- Modify: `control_plane/api/sandboxes.py`（解析 `cpuCount`/`memoryMB`；按**目标节点**的上限校验；
  超了具名 400；`record.cpu_count`/`memory_mb` 成为唯一真相）
- Modify: `control_plane/registry/manager.py`（`create()` 接受并落 `cpu_count`/`memory_mb`）
- Test: `tests/unit/test_controlplane_local_node_quota.py`（+ 新 `tests/unit/test_sandbox_size_ceiling.py`）

**Interfaces:**
- Consumes: Task 1 的节点记录三个上限
- Produces: 创建请求真正接受 `cpuCount`/`memoryMB`；越界文案形如
  `cpuCount 8 exceeds this node's per-sandbox maximum (4)`；`record.cpu_count` 不再是恒 1

- [ ] **Step 1: 写失败测试**：① 不传 ⇒ 取 settings 默认；② 传 `cpuCount: 2` ⇒ 记录 `cpu_count == 2`、
  下发 `cpuPercent == 200`；③ 传超过节点上限 ⇒ **具名 400**（不是静默夹取、也不是 503）；
  ④ 传 0/负/非法类型 ⇒ 具名 400；⑤ 准入台账用的维度与记录一致（N84 的那条不一致消失）
- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 跑测试确认通过**
- [ ] **Step 5: 提交**

### Task 3：worker 写三个限额（`+cpu +memory +pids`）

**Files:**
- Modify: `envd_service/runtime/sandbox_cgroup.py`（`setup()` 使能三个控制器；`attach()` 写
  `cpu.max`/`memory.high`/`memory.max`/`pids.max`；每个写完回读**逐字**比对）
- Modify: `envd_service/route_b.py` + `envd_service/executors/sandlock.py`（把 `memory_mb`/`max_processes` 一路传到 `attach`）
- Test: `tests/unit/test_sandbox_cgroup.py`、`tests/unit/test_route_b_cgroup_wiring.py`

**Interfaces:**
- `attach(*, sandbox_id, pid, cpu_percent, memory_mb, max_processes)`；
  `memory_max_for(memory_mb) -> str` = `f"{memory_mb * 1024 * 1024}"`；`pids_max_for(n) -> str` = `str(n)`
- `memory.high` 与 `memory.max` 同值（D2）；`memory.oom.group` **不写**（保持默认 0，D3）

- [ ] **Step 1: 写失败测试**：① 三个文件写完后**逐字**读回；② `+memory`/`+pids` 在腾空后成功、
  腾空前 EBUSY（与 `+cpu` 同规则）；③ **防御**：声明的额度高于读到的上限 ⇒ **具名拒绝**（与 P3
  一致：不夹取、不静默按小的跑；控制面已在 Task 2 拦过一次，这里是第二道）；
  ④ 任一写失败 ⇒ 具名拒绝且不留半箱
- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 跑测试确认通过**
- [ ] **Step 5: 提交**

### Task 4：只在 `required` 时退掉通知表里的 mmap 族与 clone 族

**Files:**
- Modify: `third_party/sandlock/crates/sandlock-core/src/seccomp_plan.rs`（按开关决定这两族是否进表）
- Modify: `envd_service/executors/sandlock.py`（把 lane 状态传进 fork 的 policy）
- Test: `third_party/sandlock/crates/sandlock-core/src/seccomp_plan.rs` 的单测 + `tests/unit/test_brief_stat.py` 同族
- 验收探针：`deploy/scripts/acceptance/probe_n82_traced_syscall_costs.py`（`--op mmap` 与 `clone`）

- [ ] **Step 1: 写失败测试**：`required` ⇒ `mmap`/`clone` **不在**通知表里；`off` ⇒ **在**（逐字断言表内容）
- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 跑测试确认通过**
- [ ] **Step 5: 记录"退表前/后"的 op 延迟读数**（N82 探针，同一台、同一负载；**这是本任务的主要证据**）
- [ ] **Step 6: 提交**

### Task 5：把 OOM / 撞 pids 变成可见事件

**Files:**
- Modify: `envd_service/runtime/sandbox_cgroup.py`（`release`/周期采样读 `memory.events`、`pids.events`）
- Modify: `envd_service/agent.py` + `control_plane/api/internal.py`（随心跳上报；控制面记一行具名 WARN）
- Test: `tests/unit/test_sandbox_cgroup.py`

- [ ] **Step 1: 写失败测试**：事件计数增长 ⇒ 上报字段出现且只增不减；`oom_kill` 增长打具名 WARN
- [ ] **Step 2: 跑测试确认失败** → **Step 3 实现** → **Step 4 通过** → **Step 5 提交**

### Task 6：文档与语义

**Files:** `spec.md`（限额表：每维度的强制者 + `memory` 从 `ENOMEM` 变 kill + `pids` 数任务）、
`docs/env-vars.md`、`docs/open-issues.md`（N84 收口、N83 Phase 2 行）、
`docs/production-deployment-requirements.md`（supervise 也占任务数）、`docs/deploy-clusters.md`（发版节）

- [ ] **Step 1: 逐处改**（每处都要带实测出处；N84 那条要么收口要么改写状态）
- [ ] **Step 2: 跑文档钉子** `pytest tests/unit/test_docs_only_point_at_repo_artifacts.py -q`
- [ ] **Step 3: 提交**

### Task 7：验收（本地车道 → 两段式上线）

**Files:** `deploy/scripts/acceptance/cgroup_acceptance.py`（加三条内存/进程检查）

- [ ] **Step 1: 加检查**：⑥ 箱内分配超预算 ⇒ **进程被 SIGKILL**，而**同节点另一个沙箱不受影响**
  （邻居命令往返不掉速）；⑦ fork 炸弹在箱内撞 `EAGAIN`，同节点另一个沙箱仍正常；⑧ 请求**超过集群上限**
  ⇒ 建箱**具名 400**；⑨ `memory.peak`/`pids.current` 与声明值一致
- [ ] **Step 2: 本地 compose 车道全绿**（按 `AGENTS.md`；GREEN + RED 两档，RED 仍逐条具名失败）
- [ ] **Step 3: 重建 + 两段式上线**（先 `off` 滚完冒烟 → 再翻 `required`）+ 线上复验 ⑥⑦⑧
- [ ] **Step 4: 记档**（版本、读数、坑；含"混版本窗口"那条既有纪律）
- [ ] **Step 5: 提交**

## 本计划不做

| 项 | 裁定 |
|---|---|
| 磁盘容量限额 | **不做**：cgroup v2 **没有空间配额**（`docs/disk-quota-options.md` G 行）；`io.max` 是带宽不是容量、且 NFS 上未验证。另立项 |
| `io.max`/`io.weight` | **不做**（同上，NFS 上是否生效未验证） |
| `oom.group=1`（整箱一起死） | **默认不做**（D3）；留作一行可配置 |
| 把 `memory.max` 做成"回 ENOMEM" | **不做**（P2：接受 SIGKILL） |
| 在 `off` 的部署上退通知表 | **不做**（D7） |

## 风险与开放问题

1. **语义变化要对外说清**：今天"申请内存失败"→ 之后"进程被 SIGKILL"。`spec.md` 与
   SDK 使用者的预期都要更新（Task 6 的钉子是"这条必须写"）。
2. **`pids` 数任务**：默认 256 对 JVM/Node 可能偏紧；要不要按语言给建议值，留到文档里给
   "怎么调"的指引，不在这版自动调。
3. **聚合 vs pod 限额**：Σ 各箱 ≤ 节点台账（`E2B_NODE_MEMORY_MB`）与 pod 的 4 GiB 是两套账；
   P3 只钉了"单箱不超过集群那份"，**聚合那一层**仍是既有准入的职责（今天两者也没对齐，见 N84 的邻居问题）。
4. **退表后的 argv-safety**：clone 族能否退要看 argv-safety 的前置（Phase 1 文档 §4 已记）；退不掉就
   只退 mmap 族，并在任务里写明哪一半没退、为什么。
