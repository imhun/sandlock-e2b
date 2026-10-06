# N83 Phase 1：每沙箱一个 cgroup（实施计划）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> （推荐）或 superpowers:executing-plans 逐任务实施本计划。步骤用 `- [ ]` 复选框跟踪。

**Goal:** 让"一个沙箱花了多少 CPU"由**内核记账、内核强制** —— 每沙箱一个嵌套 cgroup
（`cpu.max` **含 supervisor**），洪泛与自旋花的是沙箱自己的额度，超了像任何 CPU 密集负载一样被
节流；通知限流因此从"替 supervisor 记账"的位置退成冗余背板。

**Architecture:** 每节点的 root 组件（c3-agent **面 B**）拿到一块 **rw 的 cgroupfs 视图**，在
worker pod 的 cgroup 之下建 `sbx_<sandbox_id>` 子 cgroup，并在**现有 `grant-slot` 这一步里**
完成"建 cgroup → 写限额 → 回读校验 → 把槽位进程放进去 → **再**授予身份"。槽位子进程在身份落盘前
不会 `exec`（`slot_identity._await_identity` 轮询），而 fork 只能发生在 exec 之后 ⇒ **放置早于
任何 fork，TOCTOU 按构造关闭**。⚠ **这段是定案前（D2/D3 版本）的写法**：那时设想"worker 零 cgroup
写路、写走 CP→agent"。**Task 1 的实测推翻了它** —— 放置只能由处在 worker cgroupns 里的进程做，所以
worker 必须拿一块**收窄的** rw cgroupfs 视图并**自己**建/写自己的 `sbx_<id>`；agent 只做一次性委派。
真实形状以 §3.2 与 §4 为准。

**Tech Stack:** Python 3.14（c3-agent / control-plane / worker）、cgroup v2（节点内核 6.12）、
k8s 1.36 / k0s、containerd 2.3。

**Spec:** `docs/open-issues.md` 的 N82 与 N83 行（限流器按通知条数计费、supervisor 的 CPU 不记在
沙箱账上、"干脆不限流"实测）；现场发版与混版本事故 `docs/deploy-clusters.md` §7.46；备选形状
（supervisor 自记账 + 无条件 arm `max_cpu`）见 N82 与 `docs/resource-contention.md` §6。

**本文件的状态：2026-10-06 定稿。** 相对上一版（commit `5cd8b49`）的改动集中在三处，都是**代码与
集群实测**逼出来的：

> ⚠ **2026-10-06 晚：Task 1（探针）已跑完，读数推翻了下面的 D2/D3。** 现定案：**形态 W**
> （worker 自管沙箱 cgroup 子树，agent 只做一次性委派 —— §3.2）+ **worker 侧用 subPathExpr 收窄
> （QoS 段写死）并配两道保险**（§3.5）；架构见 §4，实测依据见 §1.3/§1.4。另一条路（形态 F：
> 自记账、零新权限）留作退路（§3.3）。原文保留，因为"为什么当初那样想"是这次改动的上下文。

1. **槽位不是 agent spawn 的**（上一版这么写过）。实测代码：worker 自己 `clone3(CLONE_NEWUSER)`
   （`envd_service/slot_identity.py::_clone3_new_user_namespace`），agent 只写 `uid_map`
   （`c3_agent/priv/as_uid.c`）。所以放置用 `cgroup.procs` 写入，且**顺序即安全**（见
   Architecture），**不需要** `clone3(CLONE_INTO_CGROUP)`。
2. **路径反查需要一个 host cgroup namespace**（上一版假设私有 cgroupns 也能反查）。实测：
   agent 私有 cgroupns 下 `/proc/<worker pid>/cgroup` 给的是 `0::/../../pod<uid>/<container-id>`
   这种**带 `..` 的相对路径**，无法与宿主挂载根拼接（§1.2）。定案 = agent pod 开
   `hostCgroupNamespace: true`（Task 1 探针复核）。
3. **子 cgroup 放哪儿是一个真问题**（上一版直接假定"挂在 worker 容器 cgroup 下"）。实测：
   worker 容器 scope 的 `cgroup.type=domain`（有进程）、`cgroup.subtree_control` 为空 ——
   按 cgroup v2 的 no-internal-process 规则，它**不能**为自己的子节点启用 cpu 控制器，写进去的
   `cpu.max` 会**静默不生效**。Task 1 用一次探针在三种放置形态里定一支（§3），判据已写死。

## Global Constraints

- **不给 worker 任何 capability、不给 `privileged`（`CapEff` 必须是 0）。** 今天实测
  `uid=65534`、`CapEff=0`、`/sys/fs/cgroup` 是 `ro,nosuid,nodev,noexec` —— 这三条保持不变。
  ~~也不给它任何 cgroup 写视图~~ ⇒ **2026-10-06 实测修正（§1.4）**：放置只能由处在 worker
  cgroupns 里的进程做，所以 worker 必须拿到一块 **rw 的 cgroupfs 视图**，且**收窄到本 pod 的
  子树**（`subPathExpr`，§3.5；**这条收窄只对 worker 成立** —— 面 B 自己的挂载仍是整节点，见
  §5 抬头）。它的写权由内核按 cgroupns + DAC 限定在**"被委派给它的 uid 的那些
  cgroup"**内：实测对**未委派**的 cgroup（含别的 pod 的）一切写操作都是 EACCES，对它自己容器的
  `cpu.max` 也是 EACCES（委派不含它）。
  ⚠ **2026-10-06 本地车道实测补充（Task 7 验收，随后由 compose 收窄修掉）**：这条边界**靠的是收窄**，
  不是靠 uid —— compose 车道上三个 worker 同宿主、**共用 uid 65534**、当时挂载是整棵树，于是每台能对
  **别的 worker 被委派的**容器 cgroup 写 `cgroup.procs`/`cgroup.subtree_control`/`mkdir`
  （`cpu.max` 仍 EACCES）。那是**当时**的车道形状，读数原样保留在
  `docs/reports/n83-task-7-cgroup-acceptance.md` 的 F1/R3 与 `docs/deploy-clusters.md` §7.48 坑 4。
  **compose 车道当天也收窄了**（2026-10-06，本文档 §3.5 的 compose 版：`cgroup_parent:
  /e2b-${COMPOSE_PROJECT_NAME}-worker-<n>` + 同名的、`volume` 直接指向这一片的 bind）——注意 compose
  **有** `volume.subpath`，但它对 `type: bind` 是**静默忽略**的（实测：不存在的 subpath 照样挂上源根、
  无告警），且它是解析期插值、命名不了容器 id，所以静态父切片才是这条车道的收窄装置。今天三条 compose
  车道的挂载只看得到自己那一片，peer 容器根本不在挂载命名空间里（复验见 §7.48）。
- **agent 在形态 W 下只多一件事**：一次性委派（chown worker 容器 cgroup 目录 +
  `cgroup.procs`/`cgroup.subtree_control`，**不碰 `cpu.max`**，**也不含 `cgroup.kill`** ——
  R9/接口层只说这三条；理由见 §3.2 的 `delegate_worker_subtree`）。不再需要
  "建 cgroup / 放进程 / 读用量 / kill" 四个 op（那套随 D2/D3 作废）；路径仍由它自推（QoS 无关），
  与 `priv_common.c` 的白名单纪律一致。
- **fail 方向钉死 closed**：建不出 cgroup / 写不上限额 / 回读不一致 / 放进程失败 ⇒ **不写
  `uid_map`** ⇒ 子进程按 `E2B_SLOT_IDENTITY_WAIT_TIMEOUT_S` 超时退出 ⇒ 建箱失败。绝不
  "无额度放行"。代价是 agent 成为建箱硬依赖 —— 它本来就是（槽位身份与文件操作都走它）。
- **每箱 cgroup 必须嵌套在 worker pod 的 cgroup 之下**（取二者较小）。**不新建节点级
  `sandlock.slice`**：那会失去 pod 的限额兜底，也要重做 pod 用量记账。
- **沙箱侧零新面**（今天实测：`/proc/self/cgroup` EACCES、`ls /sys` EACCES、mountinfo 里 0 条
  cgroup）。Phase 1 不加任何挂载/接口给沙箱，也不改这三条。
- **cgroup 不做磁盘与网络额度**：磁盘是 NAS 账本 + `RLIMIT_FSIZE`，网络是策略/代理。
- **代码默认 inert**：`E2B_SANDBOX_CGROUP=off`（不建 cgroup、不设限额，行为与今天逐字相同）；
  `required` 只在部署清单里写死（与 idle-pause 三个数同一纪律）。`required` 的语义 = **自检过不去
  就拒绝启动/拒绝建箱**（§3.5 保险 2），不是"尽力而为"。

## Review Focus

（spec 暗示、但任何单条任务的测试都不天然覆盖的五类输入/失败模式；每条都在下面某个任务里钉了测试）

1. **挂载给错了**（缺 rw 视图、`subPathExpr` 的 QoS 段与实际 QoS 不一致 —— 后者实测会**静默**
   挂到一个 kubelet 现造的假 cgroup 上）⇒ worker 必须**启动自检 + 具名拒绝**，不能照旧跑起来。
   测试：§3.5 保险 2（自检）+ 保险 1（CI 钉子）。
2. **`subtree_control` 没下发 ⇒ `cpu.max` 静默失效**（最危险的 fail-open）⇒ 必须"写完回读 +
   自旋探针证明真的被节流"，把"限额真的生效"当验收判据。探针在 Task 1，回读在 Task 3，端到端在
   Task 6 验收②③。
3. **混版本（§7.46.1 的教训）**：worker 镜像与清单不同批 —— 例如新镜像 + 旧清单（没有那块挂载）
   或旧镜像 + 新清单。方向必须**具名 fail-closed**：开关为 `required` 时自检过不去就拒启动/拒建箱，
   绝不静默退化成"无额度运行"。测试：自检的两个分支（有/无委派目录）+ 开关两个方向。
4. **没有槽位的形态**（in-process mediator；`E2B_SANDBOX_ROUTE_B=off`/`auto` 回落）不进 cgroup ⇒
   开关为 `required` 时必须**拒绝建箱**，而不是静默放行。测试在 Task 4/5。
5. **收尾的幂等与竞态**（沙箱已 kill、目录已被 GC、agent 重启、pid 已回收）⇒ release 必须幂等且
   具名（"找不到"是成功，"在但删不掉"是具名拒绝）。测试在 Task 3。

---

## 1. 现场事实（实测）

### 1.1 2026-10-06 早先的读数（决定方案形状，保留自上一版）

| 事实 | 读数 |
|---|---|
| 沙箱的进程在哪棵 cgroup | **worker pod 的容器 cgroup**（`…/pod<uid>/<container-id>`）：`sandlock-superv`、`sandlock-init`、payload 三者同 uid（池 uid）、同 cgroup |
| worker 能不能建子 cgroup | **不能**：`/sys/fs/cgroup` 挂载是 `ro,nosuid,nodev,noexec`（`cgroup.controllers` 有 `cpu io memory pids`，`cpu.max=400000 100000`） |
| agent（面 B，root）能不能建 | **不能**：私有 cgroupns；`/proc/1/root/sys/fs/cgroup` 被拒（**刻意没有 `CAP_SYS_PTRACE`**） |
| agent 能读什么 | 任意 `/proc/<pid>/cgroup` 与 `/proc/<pid>/stat`（0444，hostPID）⇒ 路径不必拼名字，从 pid 反查 |
| 沙箱自己的额度今天强制了吗 | **没有**：fork 只在 `max_cpu < 100` 时才 arm 节流，而 E2B 传 `min(100, max(1, cpu_percent))`、默认 100 ⇒ 不节流。实测 4 个自旋把 worker pod 拉到 **3832 mcore** |

### 1.2 本次定稿新增的集群只读实测（2026-10-06）

在版读数：worker 与 control-plane `0.1.0-1046-gd2d669b`、agent `0.1.0-1044-g489d6e0`；
`e2b-worker-0` 在 `.80.94`、`e2b-worker-1` 在 `.80.140`。采集方式：`kubectl exec` 进**现有**
worker / agent 容器**只读**读取（无任何写操作）。

| 事实 | 读数 | 意味着 |
|---|---|---|
| worker 容器 scope 的 `cgroup.type` | **`domain`**（`cgroup.events` = `populated 1`，`cgroup.procs` 有 3 个 pid：1/77/83） | 它有**自己的进程** ⇒ no-internal-process 规则下**不能**给它启用 cpu 控制器 |
| worker 容器 scope 的 `cgroup.subtree_control` | **空** | 子 cgroup 的 `cpu.max` 现在会是**不生效**的（这正是必须回读的原因） |
| worker 容器 scope 的 `cgroup.controllers` | `cpuset cpu io memory hugetlb pids rdma misc dmem` | **pod slice 已经把这些委派给了容器 scope** ⇒ 候选 C 缺的只是"建在 scope 里"这一步 |
| worker 容器 scope 的 `cpu.max` | `400000 100000`（4 核） | 容器级限额在 scope 上；节点 `cpu=4` ⇒ **这个 pod 的限额等于整节点** |
| agent（面 B，私有 cgroupns）看 worker pod 的进程 | `0::/../../pod203d57f0-…/<container-id>`、`0::/../../../besteffort/podcf54a692-…/<cid>` | **带 `..` 的相对路径**，无法与宿主挂载根拼接 ⇒ 反查需要 host cgroup namespace |
| agent（面 B）看自己 | `0::/`，`ls /sys/fs/cgroup` 只有 cgroupfs 文件本体 | 私有 ns 的根就是自己的 container scope（"看不到 `kubepods*`"成立） |
| 节点容量 | 两台都 `cpu=4`、`mem≈7.3 GiB` | "pod 4 核" == "整节点 4 核"；§3 的候选 C 在**本集群**不损失节点级保护 |

### 1.3 Task 1 探针（2026-10-06 已跑）—— 全部是实测，不是推理

探针：`deploy/k8s-k0s/cgroup-probe-job.yaml`（2 副本、podAntiAffinity 一节点一份）+
`deploy/scripts/acceptance/probe_cgroup_placement.py`；另有两个一次性诊断 Pod（只量"是不是只有
privileged 才有 host cgroupns"和"setns 到 worker 的 cgroupns 之后会发生什么"，未入库）。
**跑完集群已恢复原样**（worker 容器 cgroup `subtree_control` 为空、无子目录；沙箱 0 个；pod 数 9）。

| # | 问题 | 实测读数 |
|---|---|---|
| F1 | k8s 里怎么拿 host cgroupns？ | **只有 `privileged: true`**（`0::/kubepods/besteffort/pod<uid>/<cid>` + `rw` 挂载）。`hostCgroupNamespace` **不是 k8s 字段**（服务端 OpenAPI 里没有，探针 Job 被 strict decoding 拒过） |
| F2 | 面 B 形状（root+三能力、**私有 cgroupns**）+ **rw hostPath cgroupfs** 能做什么？ | 看得到宿主整棵树（`kubepods/`、`system.slice/`）；`mkdir` ok；`chown` 目录**与其中每个 kernfs 文件** ok；写 `cpu.max` 后回读逐字相等 |
| F3 | 同一个形状能把 pid 放进去吗？ | **不能**：写 `cgroup.procs` = **ENOENT** —— 目标不是它 cgroupns 根的子孙 |
| F4 | worker 形状（65534、私有 cgroupns、**ro** 挂载）呢？ | 写 `cgroup.procs` = **EROFS**；`clone3(CLONE_INTO_CGROUP)` = **EPERM**（把目录 chown 给 65534 之后仍然 EPERM） |
| F5 | 父 cgroup 还有进程时能给它 `+cpu` 吗？ | **不能**：**EBUSY** |
| F6 | 腾空之后呢？ | **ok**；而"有进程 + 已启用 domain 控制器"的 cgroup 是 **domain invalid**，此后往它子孙迁移 = **EOPNOTSUPP(95)** |
| F7 | 在**自己的** cgroupns 里（源与目标都在 ns 根之下）+ rw 视图，能放进程吗？ | **能**：`ok`，`/proc/<pid>/cgroup` 读回 `0::/sbx_selftest` |
| F8 | 限额真的生效吗？ | **生效**：0.1 核跑 3 s ⇒ `usage_usec=310453`、`nr_throttled=30`、`throttled_usec=2.70 s`；没限额的同一窗口是 ~3 000 000 µs、`nr_throttled=0` |
| F9 | 收尾与撤销 | `cgroup.kill`（0200）+ `rmdir` ok；`-cpu` 能把 `subtree_control` 撤回去 |

**形状（实测到的一棵树）**：`/kubepods/<qos>/pod<uid>/<container-id>`（cgroupfs driver，非 systemd
的 `.slice/.scope`）—— pod 目录 `cpu.max=400000 100000` 且 `subtree_control` 已含 cpu、自己没有进程；
容器目录 `cpu.max=400000 100000`、`subtree_control` 为空、**有**进程。

### 1.4 形态 W 的关键验证（2026-10-06，探针；这是"worker 形状到底能不能干"的直接答案）

形状：`runAsUser 65534` + `capabilities: {drop: [ALL]}`（**不加任何 capability**，实测
`CapEff=0000000000000000`、`CapBnd=0`）+ 一块 **rw 的 hostPath cgroupfs 视图** + 由 root helper
（模拟 agent 面 B）**一次性委派**的容器 cgroup 目录。探针脚本是一次性的、留在项目 `tmp/` 下（未入库），
两个 Pod 分别走"整棵宿主视图"与"subPathExpr 收窄视图"，读数逐字相同：

| 步骤 | 读数（两条路都成立） |
|---|---|
| 委派后的属主 | 容器目录 `65534:65534`、`cgroup.procs` `65534:65534`、**`cpu.max` 仍是 `0:0`**（故意不委派） |
| ① `mkdir worker/` + 把**自己**搬进去 | `ok`；父 cgroup `cgroup.procs` 变空 |
| ② 腾空后给父 cgroup `+cpu` | `ok`（`subtree_control: cpu`）—— 有进程时是 EBUSY，腾空是**必须**的 |
| ③ `mkdir sbx_probe` + 写 `cpu.max` | `ok`，读回 `10000 100000`，新文件属主 `65534:65534`（内核按创建者给） |
| ④ fork + 把子进程写进 `cgroup.procs` | `ok`，`/proc/<pid>/cgroup` = `0::/sbx_probe` |
| ⑤ 3 s 自旋后的 `cpu.stat` | `usage_usec_delta=315825`、`nr_throttled_delta=31`、`throttled_usec_delta=2.75 s` ⇒ **ENFORCED: true** |
| ⑥ 收尾 | `cgroup.kill`/`rmdir`/`-cpu`/把自己搬回/`rmdir worker` **全 ok**，`subtree_control` 回到 `""` |
| **负例 N1**：写自己容器 cgroup 的 `cpu.max`（root 所有） | **EACCES** ⇒ **委派不含 `cpu.max`，worker 抬不了自己的 CPU 上限** |
| **负例 N2**：对**别的 pod**（生产 worker-0）的容器 cgroup 读写 | 读 `cpu.stat`/`memory.current`/`cgroup.procs` **可以**（聚合统计级）；`mkdir`、`cpu.max`、`cgroup.procs`、`subtree_control` **全部 EACCES**（对方目录是 `0:0`） |

**subPathExpr 收窄（可用，但 QoS 段写死有真代价 —— 2026-10-06 专门测过）**：
`hostPath: /sys/fs/cgroup/kubepods` + `subPathExpr: burstable/pod$(POD_UID)`（`POD_UID` 取
downward API 的 `metadata.uid`）能正常挂载，挂载根就是本 pod 的 cgroup 目录，整条链（①–⑥）从这条
路径走也全通；收窄之后 N2 的"读别人的统计"随之消失。**但 QoS 段与 pods 的实际 QoS 一旦不一致，
失败形态是"静默挂错 + 在节点上漏一个 cgroup"**（实测：把段写成 `guaranteed/` 的那个探针）：

* **它不报错**：kubelet 会 `mkdir` 出缺失的路径 —— 在 cgroupfs 上那就是**凭空创建一个 cgroup**。
  实测读数：pod 真身在 `burstable/pod<uid>`（04:28:44，两个容器子目录），kubelet 在 04:28:45
  另建了一个 `guaranteed/pod<uid>`（**空的、没有 cpu.max**），挂到 `/pod`；pod `1/1 Running`，
  **零事件**；
* worker 于是对着**一个不是自己的 cgroup** 干活。跨 cgroupns 的迁移会被内核挡成 ENOENT，所以不会
  真把沙箱放出去；本计划 §3.2 步骤 2 的"被委派成 65534 的那个目录必须出现"恰好会在这里**失败**
  （假目录是 root 所有）⇒ 现象是"worker 起不来/建不了箱"，而不是"静默无额度"；
* 那个假 cgroup **会留在节点上**（cgroup 不会自己消失，我们手工 `rmdir` 掉的），每次 worker 重建
  漏一个。

另外两条相关的脆点：QoS 是由 resources 推出来的（`Guaranteed` = 所有容器 requests==limits；
`BestEffort` = 都没写；否则 `Burstable`），**改 resources 就会改 QoS**；而 1.36 上已有 pod 级
`spec.resources`（`kubectl explain pod.spec.resources` 实测存在），一旦用上它，QoS 的推导还会更绕。

**本地 lane 实测（2026-10-06，本机 Docker VM = cgroup v2，root 已委派全部控制器）**：worker 形状
（65534 + rw cgroupfs 视图 + 被委派的容器 cgroup）在同一台开发机的本地 lane 上**整条链跑通**：
委派 → 腾空（父 `cgroup.procs` 读回为空）→ `+cpu` → 由 **65534 自己** `mkdir sbx`（新 kernfs 文件
属主 = 创建者 = 65534）→ 写 `cpu.max` 读回 `10000 100000` → 放进程 → 4 s 自旋 ⇒
`usage_usec delta=409404`、**`nr_throttled delta=41`**；负例与 k8s 一致：65534 打开自己容器的
`cpu.max` = **Permission denied**。⇒ **cgroup 的机制部分可以在本地验，不必拿线上当试验场**
（验收顺序见 `AGENTS.md` 与 Task 7；探针是一次性的，留在项目 `tmp/`，未入库）。
**顺带量到一条委派细节**：迁移的"公共祖先"（= worker 容器 cgroup 本身）的 `cgroup.procs`
**也必须在委派列表里**，否则放置是 `EACCES`（本地实测：漏掉它时 placement 失败，补上即通过）——
§3.2 步骤 1 的列表里本来就有它，这条是它的理由。

**定案（2026-10-06，用户拍）：仍然写死**（收窄换来的"零只读暴露"值得），但**必须配两道保险** ——
清单形状、CI 钉子、启动自检与残留风险见 **§3.5**。

## 2. 定案（这份计划的决定，不再开放）

| # | 决定 | 理由 |
|---|---|---|
| D1 | 每沙箱**一个** cgroup，目录名 `sbx_<sandbox_id>`，嵌套在 worker **容器** cgroup 之下（与 `worker/` 同级，§3.2 的图） | 保留 pod 兜底（容器 cgroup 的 `cpu.max` 对子 cgroup 仍生效）；目录名可被两侧独立校验 |
| ~~D2~~ | ~~执行者是 c3-agent 面 B~~ | **已作废（Task 1 实测：agent 拿不到可写的跨 ns 放置能力，见 §3）** |
| ~~D3~~ | ~~agent pod 开 `hostCgroupNamespace: true` + rw hostPath 只挂给面 B~~ | **已作废（该字段在 k8s 里不存在，见 §3.1）** |
| D4 | 放置由 **worker** 在 spawn 槽位子进程之后立刻做：建 `sbx_<id>` → 写 `cpu.max` → 写 `cgroup.procs` → 读回 `/proc/<pid>/cgroup` 校验 | 子进程在身份落盘前不会 exec/fork，而身份是 CP→agent 事后写的 ⇒ 放置总发生在"它还没能 fork"之前，TOCTOU 按构造关闭，不需要 `CLONE_INTO_CGROUP`（§3.2 步骤 4） |
| D5 | 额度来源 = **worker 自己手里的 record**（`cpu_percent` 已经在 worker 侧），不新增 CP→worker 的下发通道 | 形态 W 下 agent 不碰限额；CP 也不需要为 cgroup 加任何 op（原 D5 的"随 grant-slot body 下发"作废） |
| D6 | Phase 1 **只接 `cpu.max`**；`memory.high/max`、`pids.max` 走 Phase 2（同一个 op，加字段，不重构） | 先把 N82 那条"洪泛花自己的额度"落地；内存/进程数各自有独立的语义变化要单独验收 |
| D7 | 开关 `E2B_SANDBOX_CGROUP` = `off`（代码默认）/`required`（清单取值），**在 worker 侧**：`required` 而启动自检过不去（§3.5 保险 2）⇒ 具名拒绝启动，绝不"无额度放行" | 形态 W 下限额的路径没有 CP→agent 参与，护栏落在"能不能自检通过"这一条上（原 D7 的 CP 校验作废） |
| D8 | 收尾由 **worker 自己**做：`cgroup.kill` + `rmdir`（幂等，找不到 = 成功）；孤儿由 GC 兜底 | 形态 W 下 cgroup 是 worker 自己建的，收尾不该绕道 CP→agent（原 D8 的 `release-cgroup` op 作废） |
| D9 | 沙箱侧与 worker 侧的可见面**一条不增** | §1.2 已实测其在沙箱内零可见、零可达 |
| D10 | 通知限流器**本计划内不动**。验收三条全过之后，另起一拍把它降级为"高额度背板"或退役 | 先让内核限额成立，再谈拆闸门 |

## 3. Task 1 结论：形态必须改成"worker 自管沙箱 cgroup 子树"

### 3.1 被推翻的两条（原 D2/D3 作废）

1. **`hostCgroupNamespace: true` 不是 k8s 字段**（F1）。要 host cgroupns 只能 `privileged: true`
   —— 那是 C3 的红线，不作数。所以"agent 用宿主视图直接建 cgroup 并放进程"这条路从根上不成立。
2. **即使给 agent 一块 rw 宿主 cgroupfs 视图，它也放不了进程**（F3：ENOENT）。放置的可行条件是
   **写者的 cgroupns 覆盖源与目标**（F7）—— 而在生产里，唯一覆盖沙箱那棵子树的角色就是
   **worker 自己**（F4：它现在既没有可写挂载，也没有被委派的目录）。

### 3.2 结论形态（W）：worker 自管，agent 只做一次性委派

```
<worker 容器 cgroup>            ← k8s 的 cpu.max = 4 核（本集群 = 整节点）；腾空后 subtree_control=+cpu
├── worker/                     ← 平台自己的进程（worker 主进程与线程）一次性迁进来
└── sbx_<id>/   cpu.max = 声明额度（含 supervisor）  ← 槽位进程由 worker 自己放进来
```

| 步 | 谁 | 做什么 | 凭据 |
|---|---|---|---|
| 1 | **agent**（面 B：root + CHOWN/DAC_OVERRIDE/FOWNER，加一块 rw hostPath cgroupfs） | 委派 worker 容器 cgroup 的**目录**（要 `mkdir`）+ **`cgroup.procs`**（迁移的"公共祖先"要可写）+ **`cgroup.subtree_control`**（要写 `+cpu`）给 65534 —— 本形态下 agent 的唯一新权力。`worker/` 与 `sbx_<id>` 由 worker 自己建，内核按创建者给属主，不必预先 chown。**`cpu.max` 故意不委派**（保持 root） | F2：chown 都 ok；§1.4 负例 N1：不委派 `cpu.max` ⇒ worker 写它 = EACCES；本地 lane 实测：漏掉 `cgroup.procs` ⇒ 放置 EACCES（§1.4） |
| 2 | **worker**（加一块 rw hostPath cgroupfs，**用 subPathExpr 收窄到自家 pod 子树** —— 定案与保险见 §3.5；运行时那块 ro 的 `/sys/fs/cgroup` 不动） | `mkdir worker/`，把 `cgroup.procs` 里的 pid 逐个写进 `worker/cgroup.procs`（腾空） | F7 + §1.4 ①②：把自己的 pid 迁进子 cgroup、腾空后 `+cpu`，两条挂载路都成立 |
| 3 | worker | **腾空之后**写 `<容器 cgroup>/cgroup.subtree_control = +cpu` | F5/F6：有进程时 EBUSY，空 cgroup ok |
| 4 | worker | 建箱：`mkdir sbx_<id>` → 写 `cpu.max` → spawn 槽位子进程后把 pid 写进 `sbx_<id>/cgroup.procs` → 读回校验 | F2/F7/F8；新建的 kernfs 文件属主 = 创建者（65534），worker 自己就能写 |
| 5 | worker | 收尾：`cgroup.kill` + `rmdir` | F9 |

**为什么这条比原计划更窄**：worker 的 cgroupfs 写权被**内核 DAC 限定在"被委派给它的 uid 的 cgroup"**
（自己的容器 cgroup + 它自己建的 `sbx_*`）—— 未委派的 cgroup 目录仍是 root:0755，它写不了；
它也没有 CAP_CHOWN，抢不走。原计划的 agent 方案里，agent 拿的是**整节点**的 rw 视图，收窄**只能靠
代码白名单**；本形态把 **worker 侧**的收窄交给内核 + **挂载收窄（`subPathExpr`）**。
**注意这条只对 worker 成立**：面 B 自己的 rw 挂载仍指向**整节点**（`hostPath /sys/fs/cgroup` →
`/host-cgroup`，k8s 与三条 compose 车道都是；`test_agent_face_b_carries_its_own_writable_cgroup_view`
钉着），它那一侧的收窄**仍然只有** op 的派生路径白名单 —— 见 §5 抬头。
**代价**：worker 从此对**自己 pod 的**沙箱子树有写权（它能节流/杀的都是它自己的沙箱 —— 它本来就能
kill 它们）。这条与 Global Constraints 里"不给 worker 任何 cgroup 写视图"**冲突**，所以需要人拍。
**⚠ 收窄是这条边界的一半**（2026-10-06 本地车道实测）：compose 车道上三 worker 同宿主同 uid 65534、
**当时**挂载是整棵树 ⇒ 被委派的 **peer** 容器 cgroup 互相可写（`cpu.max` 除外）。它**不该被读成
"内核对 DAC 的约束足够"** —— 收窄（或每 worker 独立 uid）才是把边界钉回"只有自己那棵"的那一步。
**这条后续项当天就落地了**：compose 车道改走 §3.5 ①bis 的两个静态形状（`cgroup_parent` + 指向这一片
的 bind），今天三台 worker 的挂载里 peer 容器根本不可见；原读数保留在
`docs/reports/n83-task-7-cgroup-acceptance.md` 的 F1/R3 与 `docs/deploy-clusters.md` §7.48 坑 4。

### 3.3 另一条路（F）：不做 cgroup，回到 N82 的备选

- 无条件 arm fork 的 `max_cpu`（去掉 `cpu_pct < 100` 的条件，额度不再夹到 100）+ 把节流判据改成
  **沙箱 + supervisor**（supervisor 侧把 `getrusage(SELF)` 增量并入）。
- **零新权限、零清单改动**（只改 fork 与 worker 里的代码）。
- 代价：这个节流器实测是**用户态 SIGSTOP/SIGCONT 占空比**（`sandbox_throttle_cpu`，100 ms 周期），
  不是内核 CFS；不管内存/进程数；精度差。
- 收益：它同样能让"洪泛花自己的额度"（supervisor 的 CPU 计入之后，占空比会把它压下来），而且
  **不需要动 worker 的容器形态**。

### 3.4 两形态对照（这是要拍的那一下）

| | 形态 W（cgroup） | 形态 F（自记账） |
|---|---|---|
| 新权限 | worker 一块 rw cgroupfs 视图 + agent 一次性委派 | **无** |
| 改谁的形态 | worker 与 agent 的 Pod spec | 只有代码（fork + worker） |
| 强制手段 | 内核 CFS `cpu.max`（100 ms 周期，平滑） | 用户态 SIGSTOP 占空比（粗） |
| 覆盖范围 | CPU（Phase 1）；Phase 2 可接 memory/pids | 只有 CPU 的"沙箱 + supervisor" |
| 节点邻居保护 | 保留：容器 cgroup 的 `cpu.max` 对子 cgroup 仍生效（层次带宽） | 保留：本来就是 pod 限额兜底 |
| 主要风险 | worker 对自己 pod 的 cgroup 子树有写权（DAC 限定）；k8s 管理树里多一层 `worker/` | 节流粗糙；不解决内存/进程数；supervisor 的记账是采样式 |
| 还没实测的一步 | ~~在真 worker 容器里走一遍步骤 2–4~~ **已实测（§1.4）：65534 + `CapEff=0`，整条链 ①–⑥ 全通、限额真的生效，挂载收窄版也一样** | fork 侧的改动本身 |

**曾经的建议**是先 F 后 W（F 零权限、改动集中）。**2026-10-06 用户拍：直接上 W**，并且 worker 侧
的挂载采用 **subPathExpr 收窄 + QoS 段写死 + 两道保险**（下一节 §3.5 就是这条定案的配方）。
形态 F 仍留在 §3.3 作为 W 万一走不通时的退路。

### 3.5 定案：worker 侧挂载 = subPathExpr 收窄（QoS 段写死）+ 两道保险

**决定**：worker 的 rw cgroupfs 视图**收窄到本 pod 的子树**（QoS 段在清单里写死），用两道保险抵消
§1.4 量到的"静默挂错 + 节点上漏一个 cgroup"。**两道缺一不可。**

**① 清单形状**（QoS 只有一个来源，且 `subPathExpr` 只展开一个变量 —— 这一条是**已实测**的形状）：

```yaml
volumeMounts:
  - name: cgpod
    mountPath: /pod-cgroup
    subPathExpr: "pod$(POD_UID)"          # POD_UID ← downward API metadata.uid
volumes:
  - name: cgpod
    hostPath:
      # ↓ 全仓唯一的 QoS 常量：必须等于下面 resources 推出来的 QoS
      path: /sys/fs/cgroup/kubepods/burstable
      type: Directory
```

（等价写法：把 QoS 放进 env `E2B_CGROUP_QOS`、`subPathExpr: "$(E2B_CGROUP_QOS)/pod$(POD_UID)"` ——
多一个变量展开，好处是运行期代码/日志也能读到同一个值。二选一，不要两处都写。）

**①bis 三条 compose 车道的同一个收窄（2026-10-06 落地）**：compose **有** `volume.subpath`，但它在这里
**用不了**，两个理由都是实测的 —— (1) 对 `type: bind` 它被**静默忽略**（写一个不存在的 subpath，照样
挂上源根，没有任何告警；见 `docs/deploy-clusters.md` 的踩坑表）；(2) 它是**解析期**插值，展开的是
`.env`/环境变量那一层，**命名不了容器 id**（k8s 那边靠运行时 downward API 的 `POD_UID` 才做得到）。
所以 compose 车道的收窄装置是**两个静态形状**，一条车道一组：

```yaml
worker-1:
  cgroup_parent: /e2b-${COMPOSE_PROJECT_NAME}-worker-1   # Docker 建这个切片
  volumes:
    - /sys/fs/cgroup/e2b-${COMPOSE_PROJECT_NAME}-worker-1:/pod-cgroup   # ← 只挂自己这一片
```

实测（本机 Docker VM，cgroupfs driver）：容器落在 `/e2b-<project>-worker-1/<container-id>`；父切片自己
带 `cpu.max`（`max 100000`）、`cgroup.subtree_control` 已开（`cpuset cpu io memory pids`）、没有自己的
进程 —— 正好满足启动自检的两条（挂载根有 `cpu.max` + 至少一个子目录），也让 `+cpu` 不 `EBUSY`。
**`${COMPOSE_PROJECT_NAME}` 是要求，不是装饰**：同一台 VM 上的两套栈（用户自己那套与任何一次验收）
必须各有各的父切片，否则一个挂载里会出现两套栈的 `worker-1` 容器、"恰好一个被委派的子目录"这条自检
就会具名拒绝。CI 钉子（`tests/unit/test_worker_manifest_permissions.py`）逐车道断言
**bind 源 == 该 service 的 `cgroup_parent`**（`/sys/fs/cgroup` + 它），两者不许漂移。
**运维注意（2026-10-06 实测）**：`compose down`（含 `down -v`）**不会**删掉 Docker 建的这个父切片
`/e2b-<project>-worker-<n>` —— 手工 `rmdir` 或下一次**同项目名**的 `up` 复用它，两条都行（本轮收尾就是
手工 `rmdir` 那三个）。陈旧的**空**父切片**不是拒绝风险**：worker 的启动自检先按"被委派给它的 uid"筛
目录（`sandbox_cgroup.py::_owned_candidates`），空的父切片里没有这样的子目录，它只是**不整洁**；
（例外：切片里留下属主 65534 的**子目录**时会被算成第二个候选项 ⇒ `ambiguous-delegation` 具名拒绝，
那是**另一类**残留。）
危险的是"活的切片里多出来的子目录"，那才是收窄要挡的形状。

**② 保险 1：CI 钉子（防漂移）** —— 加在清单钉子测试里（`tests/unit/test_worker_manifest_permissions.py`
一族）：

1. 从 worker 的 `resources` 推导 QoS，断言它**等于** `hostPath` 里那一段（Guaranteed = 所有容器
   requests==limits 且都写了；BestEffort = 一个都没写；否则 Burstable）；
2. 断言 `subPathExpr` 里**没有**第二处字面 QoS（只许 `pod$(POD_UID)`）；
3. 断言 k0s 覆盖层 `deploy/k8s-k0s/worker-capacity.patch.yaml` 改完 resources（今天
   requests 500m/512Mi < limits 4/4Gi）之后，推导出来的类**仍是**同一个。

这样"顺手改 resources"红在 CI，而不是在节点上种一棵假树。

**③ 保险 2：启动自检（fail-closed）** —— worker 起来后、**跑第一个沙箱之前**，按顺序：

1. **廉价前置**：`/pod-cgroup/cpu.max` 必须存在，且至少有一个子目录。**假目录两条都不满足**
   （实测：kubelet 现建的 `guaranteed/pod<uid>` 既没有 `cpu.max`、也没有子目录）⇒ 立刻具名拒绝，
   不必等委派超时，报错直接点名"挂载根不像本 pod 的 cgroup"；
2. **权威判据**：等 agent 的委派落地 —— **恰好一个子目录的属主是 worker 自己的 uid（65534）**，
   有界等待（`E2B_CGROUP_DELEGATE_WAIT_S`，默认 30 s）；等不到、或出现两个 ⇒ 具名拒绝启动。
   这条同时**独立验证了 agent 的路径推导**（它走的是 QoS 无关的 `kubepods/*/pod<pod_uid>` +
   `lookup.py` 的容器 init 规则）⇒ 两侧不会以同一种方式同时错；
   （**两条车道同一条规则，搜索空间不同**：k8s 的挂载根就是 pod 目录 ⇒ 只看一层；compose 在 2026-10-06
   收窄**之前**是整棵 VM 树 ⇒ 在挂载内做一次有界走查，按容器 id（= 容器 hostname，现成的
   `container_cgroup_token`）先缩到候选，再确认它归 65534。收窄（§3.5 ①bis）之后两块挂载的根都恰好
   是本 worker 的父目录，这条走查规则**照旧通用**（收窄前的形状也仍然满足它），所以
   `envd_service/runtime/sandbox_cgroup.py` 不必分车道。别的容器的 cgroup 都是 root 所有，
   所以"恰好一个"这条判据在两条车道上都成立。）
3. 通过之后才执行 §3.2 的步骤 2–5（腾空 → `+cpu` → 接管沙箱）。

**残留风险（写清楚，不许含糊）**：若有人绕过 CI 手工改清单把 QoS 段写错 ⇒ 现象是 **worker 起不来**
（上面第 1/2 步具名拒绝）+ 节点上多一个**空的假 cgroup**（cgroup 不会自己消失，需要手工 `rmdir`；
worker 每重建一次漏一个）。这是**已知、可见、可清**的代价，不是静默失额度 —— 这也正是它必须配
自检的原因。

### 附：2026-10-06 早先的候选表（已被上面的结论取代，保留作上下文）

| 候选 | 形状 | 代价/风险 |
|---|---|---|
| **A（首选）** | 先 `+cpu` 写进 worker 容器 scope 的 `cgroup.subtree_control`，再建 `<scope>/sbx_<id>` | 需要 scope 是 `domain`（有进程）⇒ 预计 **EBUSY**；若内核允许，这是最干净的一支，pod 限额天然覆盖 |
| **B** | 把 scope 里的任务搬进 `<scope>/worker`，再 `+cpu` + 建 `<scope>/sbx_<id>` | 保住"pod 限额覆盖整棵树"；代价是动了 k8s 管理树里的容器进程位置 |
| **C（兜底）** | 建在 **pod slice** 下（`<pod slice>/sbx_<id>`，与容器 scope 同级） | `cpu` 已经被 pod slice 委派（§1.2 的 `cgroup.controllers`），**不需要任何 subtree_control 写入**；代价是容器级 `cpu.max` 不再覆盖沙箱 ⇒ 必须把容器 scope 的 `cpu.max` 镜像到 pod slice（读不到就具名拒绝） |

**探针必须产出的读数**（缺一条都不算定形）：

1. `proc_cgroup_from_worker_pid`：`/proc/<worker pid>/cgroup` 是不是**无 `..` 的绝对路径**（验证 D3）。
2. `mkdir` 在 `<scope>/sbx_probe` 与 `<pod slice>/sbx_probe` 各自的结果（`ok` / errno 原文）。
3. `enable_subtree_cpu`：`+cpu` 写进 worker scope 的 `cgroup.subtree_control` 的结果（预期 `EBUSY`）。
4. `cpu_max_readback`：写 `10000 100000`（=0.1 核）后回读到的字面值。
5. **`enforced`：在子 cgroup 里跑 3 s 自旋，回读 `cpu.stat` 的 `usage_usec` 与 `nr_throttled`**
   —— 0.1 核 3 s ⇒ `usage_usec ≈ 300000`、`nr_throttled > 0`。这是唯一能证明"限额真的生效"的读数。
6. `place_and_readback`：把一个 pid 写进子 cgroup 的 `cgroup.procs` 后，读该 pid 的
   `/proc/<pid>/cgroup` == 目标目录。

**判据**：在 ②④⑥ 全过且 ⑤ 成立的前提下 —— ③ `ok` ⇒ 选 **A**；③ `EBUSY` 且候选 B 的等价读数成立
⇒ 选 **B**；B 也不成立而候选 C 成立 ⇒ 选 **C**（并在 Task 2 里加"镜像容器限额到 pod slice"）。
**三支都不成立 ⇒ 停手**：N83 Phase 1 的形态在本内核上不成立，回到 N82 的备选（supervisor 自记账 +
无条件 arm `max_cpu`），并重写本计划。**不许"先上了再说"**。

## 4. 架构（定案形状 = §3.2 的 W + §3.5 的挂载与保险）

```
worker 容器 cgroup（k8s 的 cpu.max = 本节点核数；腾空后 subtree_control = +cpu）
├── worker/                   ← 平台自己的进程（worker 主进程与线程）—— 启动时一次性迁进来
│                                （腾空是必须的：父 cgroup 有进程时 +cpu 是 EBUSY，§1.4 实测）
└── sbx_<id>/                 ← 每沙箱一个；worker 自己建/管（agent 只做一次性委派）
    ├── cpu.max   = 声明的 cpu_count×100% 核（含 supervisor！）
    └── cgroup.procs ← 槽位子进程 spawn 之后由 worker 写入；此后 fork 的进程自动继承
```

谁在什么时候做什么：

| 时刻 | 谁 | 动作 |
|---|---|---|
| **一次性（worker 启动时）** | agent 面 B | **新增**：委派 —— 把 worker 容器 cgroup 目录 + `cgroup.procs`/`cgroup.subtree_control` chown 给 65534（**`cpu.max` 不委派** ⇒ worker 抬不了自己的上限，§1.4 负例 N1；`cgroup.kill` 也不在名单里 —— 理由见 §3.2 的 `delegate_worker_subtree`）。路径由 agent 自推（QoS 无关：`kubepods/*/pod<pod_uid>` + `lookup.py` 的容器 init 规则） |
| 同上 | worker | **新增**：启动自检（§3.5 保险 2）→ `mkdir worker/` → 把自己搬进去（腾空）→ `+cpu` |
| 建箱（第一条命令触发槽位） | worker | 已有：`clone3(CLONE_NEWUSER)` → 上报 `{sandbox_id, pid}` → CP；**新增**：`mkdir sbx_<id>` → 写 `cpu.max` → 子进程 spawn 后把它的 pid 写进 `sbx_<id>/cgroup.procs` → 读回校验。**顺序仍然安全**：子进程在身份落盘前不 exec，fork 只能发生在 exec 之后（§3.2 步骤 4） |
| 同上 | CP | **不再需要新通道**：限额来自 worker 自己的 record（`cpu_percent`），CP 不下发、不校验 |
| 沙箱运行中 | 内核 | CFS 带宽按 `cpu.max` 节流整棵子树（supervisor 也在内） |
| kill / TTL 拆除 | worker | **新增**：`cgroup.kill` + `rmdir`（幂等；孤儿由 GC 兜底） |

## 5. 安全风险（2026-10-06 实测后写死；批准 Phase 1 时要一起看）

> **形态 W 定案后的更新（读本节前先看这段）**：下面的风险账要**分两种挂载形态读**，不能一锅端成
> "随 §3 作废"。
>
> - **worker 侧**：写权被 cgroupns + DAC 限定在自家容器子树（对别的 pod 全 EACCES），且 k8s 上用
>   `subPathExpr` 把挂载收窄到本 pod（**compose 车道 2026-10-06 用 `cgroup_parent` + 同名 bind 达到
>   同一形状**，见 §3.5 ①bis）—— "能读同节点其它 pod 的 cgroup 聚合统计"那一条确已随 §3.5
>   收窄去掉（§1.4 探针量过）。收窄的说法**只在这半边成立**。
> - **agent 面 B**：**仍然持有整节点的 rw cgroupfs 视图** —— 面 B 挂的是 `hostPath
>   /sys/fs/cgroup` → `/host-cgroup`（k8s `deploy/k8s/c3-agent.yaml` 与三条 compose 车道
>   `multinode`/`prod`/`stack/prod` 都如此，`test_agent_face_b_carries_its_own_writable_cgroup_view`
>   钉着）。面 B 是 root + `DAC_OVERRIDE` + 整棵树 rw ⇒ **本节第 1 条风险（挂载范围）对 agent 仍然
>   活着**：它原则上能写本节点任意 pod 的 cgroup，**唯一收窄是 op 的派生路径白名单**。批准时请按
>   "agent 持整节点 rw cgroupfs 视图"来算这笔账，别按"已随 §3 撤回"来算。
>   代码里真实存在的缓解，逐条点名：`container_cgroup_in_view` 只按内核读出的**目录名**匹配、且要求
>   **命中唯一**（重名即具名 `CgroupRefusal`）、`_is_within` 保证路径不越出挂载根、**请求体不带
>   路径**（agent 自己按锚点自推，`delegate-cgroup` 不收 path 参数）、以及"不做 path 参数"这条纪律
>   本身。这些降低"被误用"的概率，但不改变面 B 手里仍是整棵树的事实。
>   **后续线索**：将来可以把面 B 的挂载收窄到 `/sys/fs/cgroup/kubepods`（或更窄的本节点
>   kubepods 子树），把这条账也收回来 —— 那是独立、可单独上线的一步，`required` 的批准者应当知道
>   有这一根杆。
>
> 本节其余内容（沙箱面零可见/零可达、fail-closed 的价值、磁盘/网络不走 cgroup）仍然成立。

**结论先说：它不给沙箱开新的逃逸面 —— 新增的风险全在"节点 root 组件多出来的一类权力"和"那块 rw
挂载的范围"上。**

**今天 agent 对沙箱进程是只读的（实测）**：`CapEff = 0x0b`（只有 `CHOWN|DAC_OVERRIDE|FOWNER`，
**没有 `CAP_KILL`/`CAP_SYS_PTRACE`/`CAP_SYS_ADMIN`**）；即便 uid 0，对池 uid（10000+）的
`sandlock-superv` 做 `kill -0` / `kill -TERM` 都是 **EPERM**，但 `cat /proc/<pid>/status` 可以。
⇒ cgroup 写权限把它从"能读、能在五根白名单里改文件"变成"**能节流、能 OOM、能杀**"——这是一类
**新的权力**，不是同类的加量。

**不新增的**：① 沙箱自己拿不到任何东西（§1.2：它的挂载命名空间里没有 cgroupfs，写不了自己的
限额；`/proc/self/cgroup` 本来就只读可看）；② pod/节点限额不被削弱（每箱 cgroup 是**嵌套**子节点，
pod 的 4 核/4 GiB 仍然生效，取二者较小）；③ 可达性不变（op 仍走 CP→agent 的 token +
NetworkPolicy，worker 敲不进来）；④ 对已被攻破的 CP 边际为零（它本来就能
`DELETE /sandboxes/{id}`）。

**真正的风险点，按严重度排**：

1. **挂载范围**：hostPath 无法指向"动态的每 pod 路径"（pod uid 每次都变），所以现实里只能挂
   `/sys/fs/cgroup`（含 kube-system 的 pod）⇒ **代码里的白名单是唯一收窄**，一个路径解析 bug
   就等于"能节流/杀掉本节点任意 pod 的进程"。
2. **白名单必须在解析后的路径上做**（`c3_agent/priv/priv_common.c` 已经踩过这个坑：符号链接/`..`
   都要先解析再比较），而且 **op 不接受 CP 传来的路径**：agent 自己由 `sandbox_id` + 目标
   worker pod uid/容器 id 拼。
3. **一个具体的 fail-open 形态**：worker 容器 scope 的 `cgroup.subtree_control` 今天是**空的**
   （§1.2 实测）⇒ 子 cgroup 里写 `cpu.max` 会**不生效**（限额静默失效）⇒ 落地时必须**建完回读**
   （`cpu.max` 读回 + 一条自旋探针），把"限额真的生效"当成验收判据，而不是写完就算。
4. **TOCTOU**：spawn 之后再写 `cgroup.procs`，窗口里 fork 出去的进程会留在 worker 的 cgroup
   （**逃出限额**）⇒ 本计划用**顺序**关闭它（D4）：子进程在身份落盘前不 exec、fork 只能在 exec
   之后，而放置发生在写 `uid_map` 之前。若实现时改成"身份先给、再放进程"，这个保证立刻失效。
5. **不要把沙箱移出 pod slice**（换成节点级 `sandlock.slice` 虽然能把挂载收窄到那一棵，但会失去
   k8s pod 的 CPU/内存兜底，也要重做 pod 的用量记账）⇒ 保持嵌套在 worker pod 之下。
6. **fail 方向钉死 closed**：见 Global Constraints。代价是 agent 变成建箱的硬依赖（它本来就是）。

**worker 面与沙箱面（2026-10-06 实测；这两面才是用户实际担心的）**

- **worker 面：`uid=65534 CapEff=0000000000000000` 保持不变；cgroup 写路是 Phase 1 新开的、
  且必须**收窄**。** 定案前它是零写路（`/sys/fs/cgroup` 是 `ro` 挂载 —— `echo $$ > cgroup.procs` 与
  `mkdir` 都是 **`Read-only file system`**）。**Task 1 的实测把它改了**：放置只能由处在 worker
  cgroupns 里的进程做 ⇒ worker 拿一块 rw cgroupfs 视图（k8s 用 `subPathExpr` 收窄到本 pod 子树；
  compose 2026-10-06 之前是整棵树、之后用 `cgroup_parent` + 同名 bind 收窄到本 worker 那一片，见
  §3.5 ①bis），由**它自己**建/写 `sbx_<id>`；agent 不再有"建/放/读/kill"四个 op，只剩
  **一次性委派**（详情 §3.2/§4）。**capability 仍然是零** —— 写权全靠内核的 cgroupns + DAC。
  ⚠ 由此产生的那条车道级风险（同 uid 的 peer 可写）见本节末尾"第四条风险"—— 它已在 2026-10-06
  当天由 compose 收窄（§3.5 ①bis）修掉，那一段的读数作为历史保留。
  **万一 worker 被攻破**：新增的是"对**本节点**同侪沙箱的**进程级**控制（节流/杀）"。**范围不变**
  —— file-op 那条已经有对象检查（`record.node_id != node_id` → 403），也就是说它今天就能对本节点
  的沙箱做数据级操作；新的是**种类**（进程 vs 数据），不是范围。缓解：cgroup op 复用同一条归属检查
  + agent 的解析后白名单 + 目标由 CP 派生（worker 不传路径、不传 uid）。
- **沙箱面：零可见、零可达（实测）。** 沙箱里 `cat /proc/self/cgroup` → **EACCES**（`/proc` 是中介
  合成的，`self/cgroup` 不在白名单）；`ls /sys` → **EACCES**；`grep -c cgroup /proc/self/mountinfo`
  → **0**；`ls /sys/fs/cgroup` → **ENOENT**。⇒ 每沙箱 cgroup **不给沙箱任何新信息、也拿不到任何
  句柄**，"目录用 sandbox_id 还是池 uid 命名"这个问题**不存在**。
  对沙箱的**收益**：每箱 `cpu.max` 把 N82 那类"邻居被吵"变成"花自己的额度" —— 这是沙箱面的安全
  **改善**（跨租户公平/DoS），不是新增风险。**功能变化**（非安全）：`cpu.max` ⇒ CFS 100 ms 周期
  节流（比通知限流那 860 ms 的一秒悬崖平滑得多）；`pids.max` ⇒ fork `EAGAIN`；`memory.max` ⇒
  OOM kill（Phase 2 的事）。

**Task 7 验收里量到的第四条风险（车道级，2026-10-06）：同 uid 的 peer 可写。** k8s 车道上
`subPathExpr` 把挂载收窄到本 pod，peer 根本不可见 ⇒ 边界成立。**compose 车道没有收窄**、三个 worker
同宿主共用 uid 65534 ⇒ 每台能对**别的 worker 被委派的**容器 cgroup 写 `cgroup.procs`/
`cgroup.subtree_control`/`mkdir`（`cpu.max` 仍 EACCES，未委派的 cgroup 仍全 EACCES，租户 payload 是池 uid
够不着）。影响面：持 worker uid 的进程可以把进程搬进 peer 的容器 cgroup 或在其下 `mkdir`（记账/归属可被
搬移 ⇒ 可规避自己容器级额度、消耗邻居预算）。**裁定**：不阻塞 Phase 1（生产形态是 k8s + 收窄），
如实写进 `docs/deploy-clusters.md` §7.48，并登记为后续项 —— compose 车道要么给每个 worker **独立 uid**、
要么也做收窄。**这条正是"收窄是边界的一半"的证据**：少了它，内核对 DAC 的约束不足以把写权钉回自己那棵。

> **2026-10-06 当天收尾（本条风险的 re-scope；上面的读数整段保留为历史）**：compose 车道**也收窄了**
> —— 三条 compose 车道（`multinode`/`prod`/`stack/prod`）的每个 worker 现在都是
> `cgroup_parent: /e2b-${COMPOSE_PROJECT_NAME}-worker-<n>` + 指向**这一片**的 bind（§3.5 ①bis；
> compose 的 `volume.subpath` 对 `type: bind` 静默无效、且是解析期插值，所以只能用静态父切片）。
> 复验（`-p n83narrow`、宿主端口 3300，`E2B_SANDBOX_CGROUP=required`）：三台 worker 的 `/pod-cgroup`
> 里**只有 cgroupfs 文件 + 恰好一个容器目录（自己的）**，`docker/`、`kubepods*` 都不存在；验收脚本
> check ④ 因此第一次走 **`narrowed-mount`** 分支（peer 可见数 **0**，证据 = 挂载根三条写全 EACCES），
> 不再是当年的 `peer-container`（15 个 peer）。原始 JSON 与读数在
> `docs/reports/n83-task-7-cgroup-acceptance.md`（本轮归档）与 `docs/deploy-clusters.md` §7.48。
> 于是**这条风险今天只在"收窄前的 compose 形状"上成立**；k8s 与今天的 compose 都满足"peer 不在挂载
> 命名空间里"。剩下的形态事实照旧：同一个宿主 uid 之所以当年能写 peer，是因为委派把 peer 容器 cgroup
> 的**目录** chown 给了 65534 —— 收窄把它变成了够不着，而不是让 DAC 学会了区分。

---

## Tasks

> **形态已定**：W（§3.2）+ worker 侧 subPathExpr 收窄 / QoS 段写死 + 两道保险（§3.5）；架构见 §4。
> Task 1（探针定形）**已执行完**，读数在 §1.3/§1.4。下面从 Task 2 起编号、按依赖排序：
> **Task 2 与 Task 3 可并行**，其余按序。每条都写清"能被独立复核"的验收。
>
> **验收顺序（2026-10-06 起，见 `AGENTS.md`）：先在本地 lane 跑绿，才允许发线上。** Task 7 的
> 步骤按这条重排 —— k0s 只用来复验"本地证明不了的形状事实"，不再当试验场。
>
> **Task 1 的产物与复跑方式**：Job 清单 `deploy/k8s-k0s/cgroup-probe-job.yaml` + 探针脚本（在
> `deploy/scripts/acceptance/` 下，按名字找 `probe_cgroup_placement`）。复跑 =
> `kubectl -n sandlock create configmap cgroup-probe --from-file=<探针脚本>` →
> `kubectl -n sandlock apply -f deploy/k8s-k0s/cgroup-probe-job.yaml` →
> `kubectl -n sandlock logs -l app=cgroup-probe --prefix` → 删 Job 与 ConfigMap（探针自建自删 `sbx_probe`）。

### Task 2：清单 —— worker 的收窄 cgroupfs 视图 + QoS 常量 + CI 钉子（§3.5 保险 1）

**Files:**
- Modify: `deploy/k8s/worker.yaml`（加 `POD_UID`（downward API `metadata.uid`）、volume `cgpod`
  （`hostPath: /sys/fs/cgroup/kubepods/burstable`）、volumeMount `mountPath: /pod-cgroup` +
  `subPathExpr: "pod$(POD_UID)"`、env `E2B_SANDBOX_CGROUP=off` 与 `E2B_CGROUP_MOUNT=/pod-cgroup`）
- Modify: `deploy/k8s-k0s/worker-capacity.patch.yaml`（把 `E2B_SANDBOX_CGROUP` 覆盖成 `required`；
  这条与 idle-pause 三个数同一纪律：代码默认关、取值在清单）
- Modify: `deploy/compose/docker-compose.multinode.yml`（**本地验收车道**：worker-1/2/3 各加
  `cgroup_parent: /e2b-${COMPOSE_PROJECT_NAME}-worker-<n>` + `/sys/fs/cgroup/e2b-${COMPOSE_PROJECT_NAME}-worker-<n>:/pod-cgroup`
  （rw）绑定 + `E2B_SANDBOX_CGROUP` / `E2B_CGROUP_MOUNT` 两个 env；
  `c3-agent-maint`（面 B）加同一块 rw 绑定 —— 委派就是它做的）
- Modify: `deploy/compose/docker-compose.prod.yml` 与 `deploy/stack/docker-compose.prod.yml`
  （同样的改动，保持两条 compose 车道与生产示例同步；`tests/unit/test_compose_base_image_shape.py`
  一族会盯住它们）
  注：**compose 有 `volume.subpath`，但绑不进来** —— 对 `type: bind` 它被静默忽略（实测：不存在的
  subpath 照样挂源根、无告警），而且它是解析期插值、展开不出容器 id。所以收窄用静态父切片
  （§3.5 ①bis）：挂载根就是本 worker 的父目录，worker 侧认"**被委派的那个目录**"这条代码在两条
  车道上照旧通用（k8s 的挂载根是 pod 目录 ⇒ 一层；compose 走一次有界走查）。
- Test: `tests/unit/test_worker_manifest_permissions.py`

**Interfaces:**
- Produces: 运行期 `/pod-cgroup`（挂载根 = 本 pod 的 cgroup 目录，实测形状见 §1.4）、
  `E2B_CGROUP_MOUNT` 的取值，以及三条 CI 断言（QoS 段 == 由 resources 推导的 QoS；
  `subPathExpr` 里没有第二处字面 QoS；k0s 覆盖层之后仍是同一类）。

- [ ] **Step 1: 写失败断言**：worker 的 mount 里有 `/pod-cgroup`；`subPathExpr == "pod$(POD_UID)"`；
  `hostPath.path == "/sys/fs/cgroup/kubepods/burstable"`；由这两处推出的 QoS == `"burstable"`；
  `worker-capacity.patch.yaml` 应用后的 resources 推导仍是 `"burstable"`
- [ ] **Step 2: 跑测试确认失败**：`pytest tests/unit/test_worker_manifest_permissions.py -v` ⇒ FAIL
- [ ] **Step 3: 改两个清单**（挂在 worker 容器上；`runAsUser`、`capabilities`、`readOnlyRootFilesystem`
  一个字都不动 —— 形状仍是"零 capability"）
- [ ] **Step 4: 跑测试确认通过**：`pytest tests/unit/test_worker_manifest_permissions.py tests/unit/test_docs_only_point_at_repo_artifacts.py -q` ⇒ PASS
- [ ] **Step 5: 本地 lane 核验（不上集群）**：本地 lane 起 worker 后
  `docker exec <worker> sh -c 'ls /pod-cgroup'` ⇒ 能看到 cgroupfs 文件**与容器目录**（视图确实挂上了）。
  k8s 侧的**收窄**形状已在 Task 1 的探针里量过（§1.4），线上阶段只在 Task 7 复验一次。
- [ ] **Step 6: 提交**：`git add deploy/k8s/worker.yaml deploy/k8s-k0s deploy/compose deploy/stack tests/unit/test_worker_manifest_permissions.py && git commit -m "feat(worker): narrow writable cgroup view for per-sandbox limits (N83 phase 1)"`

### Task 3：agent —— `delegate-cgroup`（一次性委派 + 容器 cgroup 定位）

**Files:**
- Modify: `c3_agent/lookup.py`（新增公开方法，复用现成的 `_cgroup` / `_is_container_init` / `pod_cgroup_token`）
- Create: `c3_agent/cgroups.py`（委派逻辑：**白名单 chown**）
- Modify: `c3_agent/app.py`（新 op `delegate-cgroup` + body 模型 `DelegateCgroupBody`）
- Test: `tests/unit/test_c3_delegate_cgroup.py`（新）、`tests/unit/test_c3_agent_service.py`

**Interfaces:**
- Produces（Task 4/6 依赖）：
  - `ProcLookup.worker_container_cgroup(*, node_id, pid_namespace, pod_uid=None, container_id=None) -> str`
    —— **按车道**定位 worker 容器 cgroup 目录（与 `worker_uid_gid` / `host_pid` 同一套候选规则）：
    k8s 用 `pod_cgroup_token(pod_uid)`（**QoS 无关**），compose 用 `container_cgroup_token(container_id)`；
    两边都用 `_is_container_init` 把 exec 兄弟目录排除掉。找不到 / 多于一个 ⇒ `LookupRefusal`
  - `c3_agent.cgroups.delegate_worker_subtree(*, mount: Path, container_cgroup: Path, worker_uid: int) -> tuple[str, ...]`
    —— 只 chown **三条：`.`（目录本身）+ `cgroup.procs` + `cgroup.subtree_control`**，返回被 chown
    的条目名；**`cpu.max` 必须在返回值之外**（§1.4 负例 N1 的钉子）。**`cgroup.kill` 也不在名单里**
    （R9）：worker 的 kill/收尾只落在**它自己建的 `sbx_<id>`** 上，那个目录由内核按创建者把属主交给
    它（65534），`cgroup.kill` 本来就是它的；这份委派清单只为"worker 要写它**容器** cgroup"这一件
    事开条子，多列一个 `cgroup.kill` 会让文档比实现宽（Minor 1 修正前就是这个状态）。
  - agent op `delegate-cgroup`（挂在**面 B**，与 `chown`/`rm`/`walk`/`materialize` 同一张 op 表）：
    body `{"worker": {"node_id", "pod_uid"?, "container_id"?}}` —— **锚点按车道二选一**，与
    `WorkerCredentials` 的 D21/D25 规则同形（k8s 传 `pod_uid`、compose 传 `container_id`）→
    回答 `{"op", "containerCgroup", "delegated": [...], "cpuMaxOwner": "0:0"}`

- [ ] **Step 1: 写失败测试**：① 两个候选目录（容器 + exec cgroup）时**只选含容器 init 的那个**；
  ② 歧义/找不到 ⇒ 具名 502；③ `delegated` 里**没有 `cpu.max`**；④ 挂载缺失 ⇒ 具名拒绝（不是静默跳过）；
  ⑤ **两条车道的锚点各测一遍**：k8s 的 `pod_uid` 与 compose 的 `container_id` 都能定位到容器目录
- [ ] **Step 2: 跑测试确认失败**：`pytest tests/unit/test_c3_delegate_cgroup.py -v` ⇒ FAIL
- [ ] **Step 3: 实现**（`c3_agent/lookup.py` 的新方法全部走可注入的 `proc_root`；`c3_agent/cgroups.py`
  只做"解析后路径"的白名单 chown；`c3_agent/app.py` 的 op 与 `grant-slot` 共用 token、路由与日志形状）
- [ ] **Step 4: 跑测试确认通过**：`pytest tests/unit/test_c3_delegate_cgroup.py tests/unit/test_c3_agent_service.py tests/unit/test_c3_slot_identity_lookup.py -q`
- [ ] **Step 5: 提交**：`git add c3_agent tests/unit/test_c3_delegate_cgroup.py && git commit -m "feat(c3-agent): delegate the worker's cgroup subtree, cpu.max excluded (N83 phase 1)"`

### Task 4：控制面 —— 委派握手（worker 启动时请求一次）

**Files:**
- Modify: `control_plane/c3_agent_client.py`（`delegate_cgroup(*, node_id: str) -> dict`）
- Modify: `control_plane/api/internal.py`（`POST /internal/nodes/{node_id}/cgroup-delegate`：复用
  `node_slot_identity` 那套 ①credential ②claim ③record 归属 检查，把**节点记录里的锚点**（k8s =
  `pod_uid`，compose = `container_id`）交给 client）
- Modify: `envd_service/worker_identity.py`（与 `build_identity_reporter` 同源：新增
  `request_cgroup_delegate(...)`，共用 `E2B_CONTROL_PLANE_URL` / `E2B_NODE_ID` 那套凭据）
- Test: `tests/unit/test_c3_internal_api_shape.py`、`tests/unit/test_c3_slot_identity_forwarding.py`

**Interfaces:**
- Produces: `request_cgroup_delegate(*, timeout_s: float) -> dict`（worker 侧）；CP 对"节点没有 pod_uid /
  agent 不可达 / 重复调用"的具名回答（重复调用**幂等**：agent 的 chown 本来就是幂等的）。

- [ ] **Step 1: 写失败测试**：① 节点没有 `pod_uid` ⇒ 503 具名；② agent 拒绝 ⇒ 502 具名透传；
  ③ 成功 ⇒ 回答里带 `containerCgroup` 与 `delegated`；④ 连调两次都 200（幂等）
- [ ] **Step 2: 跑测试确认失败**：`pytest tests/unit/test_c3_internal_api_shape.py -q` ⇒ FAIL
- [ ] **Step 3: 实现**
- [ ] **Step 4: 跑测试确认通过**：`pytest tests/unit/test_c3_internal_api_shape.py tests/unit/test_c3_slot_identity_forwarding.py -q`
- [ ] **Step 5: 提交**：`git add control_plane envd_service/worker_identity.py tests/unit && git commit -m "feat(cp): one-shot cgroup delegation handshake for workers (N83 phase 1)"`

### Task 5：worker —— sandbox cgroup 模块（自检 / 腾空 / attach / release）

**Files:**
- Create: `envd_service/runtime/sandbox_cgroup.py`
- Test: `tests/unit/test_sandbox_cgroup.py`（新）

**Interfaces（名字与签名照抄）**：

```python
class CgroupRefusal(Exception): ...          # 具名、fail-closed

def cpu_max_for(cpu_percent: int) -> str:    # f"{cpu_percent * 1000} 100000"
    ...

class SandboxCgroups:
    def __init__(self, *, mount: Path, worker_uid: int,
                 proc_root: Path = Path("/proc")) -> None: ...
    def setup(self, *, wait_s: float) -> str          # §3.5 保险 2 + 腾空 + `+cpu`；返回证据行
    def attach(self, *, sandbox_id: str, pid: int, cpu_percent: int) -> str
    def release(self, *, sandbox_id: str) -> bool     # 幂等：缺席 ⇒ False
```

- Consumes: Task 3 的委派（它把容器 cgroup 目录 chown 给 65534）、Task 4 的握手
- Produces: 供 Task 6 用；`mount` / `proc_root` 都可注入 ⇒ 单测不需要真 cgroupfs

- [ ] **Step 1: 写失败测试**（在 `tmp_path` 里搭一棵合成 cgroupfs，注入 `proc_root`）：
  ① `setup` 遇到"没有 `cpu.max`"的挂载根 ⇒ `CgroupRefusal`（假目录那条）；② 等不到 65534 拥有的
  子目录 ⇒ `CgroupRefusal`；③ 有两个这样的子目录 ⇒ `CgroupRefusal`；④ 正常路径：腾空（父 `cgroup.procs`
  读回为空）+ `+cpu` 写入字样**逐字**断言；⑤ `attach`：`cpu.max` 读回逐字 `"100000 100000"`、
  `/proc/<pid>/cgroup` 读回逐字、目标目录已存在且 `cgroup.procs` 非空 ⇒ 拒绝；⑥ `release` 幂等
  （缺席 ⇒ `False`），`rmdir` 失败 ⇒ 具名拒绝
- [ ] **Step 2: 跑测试确认失败**：`pytest tests/unit/test_sandbox_cgroup.py -v` ⇒ FAIL
- [ ] **Step 3: 实现**（**每个写入后回读比对**；任一步失败 ⇒ 把已建的目录清掉再抛 `CgroupRefusal`）
- [ ] **Step 4: 跑测试确认通过**：`pytest tests/unit/test_sandbox_cgroup.py -v` ⇒ PASS
- [ ] **Step 5: 提交**：`git add envd_service/runtime/sandbox_cgroup.py tests/unit/test_sandbox_cgroup.py && git commit -m "feat(worker): per-sandbox cgroup primitives with a fail-closed self-check (N83 phase 1)"`

### Task 6：worker —— 接进 route-B 与启动路径（开关 + fail-closed）

**Files:**
- Modify: `envd_service/config.py`（`sandbox_cgroup` = off|required、`cgroup_mount`、`cgroup_delegate_wait_s`）
- Modify: `envd_service/agent.py`（启动：`request_cgroup_delegate` + `SandboxCgroups.setup`；
  `_claim_host_uid` 里那个 `pool.acquire(...)` 调用点带上 `cpu_percent`）
- Modify: `envd_service/route_b.py`（`RouteBSlotPool(..., sandbox_cgroups=None)`；
  `acquire_sync(..., cpu_percent=None)` 在 `self._spawner(...)` 之后、**身份上报之前** `attach`；
  `_retire_locked` 调 `release`）
- Modify: `envd_service/executors/sandlock.py`（route-B acquire 调用点传 `cpu_percent=self._cpu_percent`）
- Test: `tests/unit/test_route_b_slot_identity.py`、`tests/unit/test_sandbox_executor_route_b.py`
  （+ 新 `tests/unit/test_route_b_cgroup_wiring.py`）

**Interfaces:**
- `off` ⇒ 全链路空操作（行为与今天**逐字相同**）；`required` ⇒ 池未就绪时 acquire 抛具名错误
  （建箱失败，绝不无额度放行）；`attach` 失败 ⇒ **身份不上报**（子进程被杀，不留半个槽位）。
- 启动期委派失败**不崩进程**：后台按 `E2B_CGROUP_DELEGATE_WAIT_S` 重试，期间拒绝建箱并具名
  （理由：CP 短暂不可用不该让 worker crashloop）。

- [ ] **Step 1: 写失败测试**：① `off` ⇒ 假 `SandboxCgroups` 的 `attach` **零调用**；② `required` 且池未就绪
  ⇒ acquire 抛具名错误；③ `attach` 失败 ⇒ 假 identity reporter **零调用**且子进程被杀；④ `release`
  被调一次；⑤ `cpu_percent` 一路传到 `attach`（断言收到的值 == **声明值**，不是 `min(100, …)`）
- [ ] **Step 2: 跑测试确认失败**：`pytest tests/unit/test_route_b_cgroup_wiring.py -v` ⇒ FAIL
- [ ] **Step 3: 实现**
- [ ] **Step 4: 跑测试确认通过**：`pytest tests/unit/test_route_b_cgroup_wiring.py tests/unit/test_route_b_slot_identity.py tests/unit/test_sandbox_executor_route_b.py -q`
- [ ] **Step 5: 提交**：`git add envd_service tests/unit && git commit -m "feat(worker): per-sandbox cpu.max wired into the route-B slot lifecycle (N83 phase 1)"`

### Task 7：集群验收 + 文档 + 回退杆
> **顺序（`AGENTS.md` 的规矩）：本地 lane 全绿 → 才发线上。** 本地 lane 能证明的范围见 §1.4
> （cgroup 机制整条链已在本机 Docker VM 上跑通），线上只复验"本地证明不了的形状事实"。

**Files:**
- Create: 验收脚本 `cgroup_acceptance`（扩展名 `.py`；落到 `deploy/scripts/acceptance/` —— 与探针同一条
  纪律：**脚本落盘与本文档的引用同一次提交**，否则文档钉子会红）
- Modify: `docs/env-vars.md`、`docs/open-issues.md`（N83 行）、`docs/deploy-clusters.md`（新发版节）、
  `docs/resource-contention.md`（§6 追加"cgroup 落地后的口径"）

- [ ] **Step 1: 写验收脚本**（五条，缺一不可；脚本**对任何 E2B endpoint 都跑得动**，所以本地 lane 与
  线上共用同一支，只把"看容器"的命令按车道参数化）：
  ① 沙箱内 4 自旋 ⇒ `measuredCpuPercent ≈ 声明额度`（声明 100 时今天是 375.5）且**同节点第二个沙箱的
  `command_rtt` 不掉速**；② 沙箱内 3 s 自旋后 `cpu.stat.nr_throttled > 0` 且 `usage_usec` 与额度同量级；
  ③ 关掉 `E2B_SANDBOX_NOTIFY_RATE_LIMIT` 后 `open+close` 洪泛仍落在额度内（量完撤回）；④ 收窄核验
  （车道专属命令：k8s = worker 内 `ls /pod-cgroup` 看不到 `kubepods/`；本地 lane = 只看得到自己容器
  那棵子树）；⑤ 负例：worker 内 `open(<自己的容器 cgroup>/cpu.max, O_WRONLY)` ⇒ **EACCES**
- [ ] **Step 2: 本地 lane 验收 —— 必须全绿，否则不许发线上**：起**本地 compose 多节点栈**
  `deploy/compose/docker-compose.multinode.yml`（本机 override 视需要叠加；worker-1/2/3 + 控制面 +
  redis + agent 两面，就是"最接近线上"的那条车道），跑 ①②③⑤ 加本地形状的 ④（本地版 ④ = worker 里
  只能看到自己容器那棵子树），把 **RED→GREEN 的读数**贴进 `docs/reports/`。这一条不通过，后面两步
  一律不做
- [ ] **Step 3: 上线（本地绿了才做）**：`git log --oneline <在版 sha>..HEAD` 看清镜像会带什么 → 构建 →
  **先让 `E2B_SANDBOX_CGROUP=off` 滚完并冒烟建箱** → 再把 `worker-capacity.patch.yaml` 里那行翻成
  `required` 并 apply → 线上只复验**本地证明不了的形状**：④（k8s 收窄）+ 一条端到端冒烟（②）。
  任何一条红 ⇒ 立刻用 Step 5 的回退杆翻回 `off`
- [ ] **Step 4: 更新文档**（env-vars 三个新变量 + 语义；N83 行改状态；`docs/deploy-clusters.md` 新发版节
  写清两次 apply 的读数与踩到的坑）
- [ ] **Step 5: 记回退杆**：`worker-capacity.patch.yaml` 里那行翻回 `off` ⇒ 回到今天的行为
  （基线本来就是 `off`；已有 cgroup 在其沙箱拆除时照常释放）
- [ ] **Step 6: 提交**：`git add deploy/scripts/acceptance docs && git commit -m "docs(N83): per-sandbox cgroup acceptance (local lane first) and rollback lever"`

### Task 8（可选，Phase 1 验收之后另起一拍）：限流器降级

- [ ] 五条验收全过之后，再把 `E2B_SANDBOX_NOTIFY_RATE_LIMIT` 从"替 supervisor 记账"降级为高额度背板
      或退役；这一步不写进 Phase 1 的提交，单独测量、单独发版。

## 本计划不做

| 项 | 裁定 |
|---|---|
| worker `privileged: true` / 整棵 cgroupfs rw 挂给 worker | **不做**。等于把"节流/杀同族 pod"的权力交给沙箱相邻组件。 |
| 用 cgroup 做磁盘/网络额度 | **不做**。磁盘有账本 + `RLIMIT_FSIZE`，网络有策略与代理；分开管。 |
| 把 supervisor 留在沙箱额度之外、继续用通知限流兜底 | **不做**。那正是 N82 测出来的"替人记账"，且一秒级悬崖不可接受。 |
| 自记账（supervisor 读 `getrusage` 并进沙箱用量） | **本轮不做**。作为 Task 1 三支全败时的备选：改动小、不引新权限，但只在**由 supervisor 执行**的路径上成立（进程内形态的记账在 worker 里），且不覆盖"沙箱自己的进程直接烧 CPU"那一半 —— 那半今天靠 `max_cpu`，而它默认没 arm。 |
| `memory.*` / `pids.max` | **Phase 2**（同一个 op 加字段）。今天先只做 CPU。 |
| 节点级 `sandlock.slice` | **不做**（见 Global Constraints）。 |
| 把 cgroup 的目录名做成池 uid | **不做**。沙箱面对 cgroup 零可见（§1.2），命名只影响平台自己；用 `sandbox_id` 可直接被 CP 侧校验。 |

## 风险与开放问题

1. **Task 1 可能三支全败**（尤其候选 A 预期 `EBUSY`）：那意味着"每沙箱 cgroup"在本内核 +
   本部署形态下不成立，计划停在那里并回到 N82 的备选 —— 这是**允许的结果**，不是失败。
2. **候选 C 的代价**：容器级 `cpu.max` 不再覆盖沙箱 ⇒ 必须"读容器 scope 的 `cpu.max` 并镜像到
   pod slice（只在 pod slice 现值为 `max` 时）"。本集群节点只有 4 核、pod 限额也是 4 核，所以差异
   为零；换到 pod 限额 < 节点核数的部署，这一步是**必须**的，不能省。
3. **in-process 形态**（`E2B_SANDBOX_ROUTE_B=off/auto` 回落）没有槽位 ⇒ 不进 cgroup。开关为
   `required` 时 CP 必须拒绝建箱（Review Focus 4 的测试）；生产形态（route B）不受影响。
4. **`cpu.max` 的语义**：额度 = `cpu_count×100%` 核（含 supervisor）。这与节点台账
   （`E2B_NODE_CPU_PERCENT=400`）同口径；今天 sandlock 侧把 `max_cpu` 夹在 `min(100, …)`，本计划
   **不沿用那个夹取**（否则 `cpu_count≥2` 的沙箱仍然只有 1 核）。
5. **暂停/恢复**（idle→pause 的 SIGSTOP）：暂停中的沙箱进程仍在 cgroup 里，`cpu.max` 保持；
   恢复不需要重建 cgroup。若将来 pause 变成"停进程 + 归还资源"的更深形态，要重新看这里。
