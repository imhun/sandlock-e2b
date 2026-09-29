# C3 特权收敛（agent 作为唯一特权组件）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 worker 从"跑沙箱 + 干特权活"降成"只跑沙箱"，让一个**非 root 优先、能力面最小**
的每节点 agent 成为全系统**唯一的身份授予者与文件操作执行者**，并把控制面收敛到**无 root**。

**Architecture:** 三角分工 —— **CP 只发指令**（零特权）→ **agent 只执行**（两个面）→
**worker 只跑沙箱**（零特权二进制）。槽位仍由 worker 自己 `fork`（保住进程树与 cgroup 归属），
身份由 agent 写一次 `/proc/<C>/uid_map` 授予（"uid_map 代写"）。

**Tech Stack:** Python 3.14（`control_plane/` `envd_service/`）、C（`deploy/priv/`）、
`third_party/sandlock`（Rust，supervise）、k8s（k0s，arm64）、NFS（阿里云 NAS，AUTH_SYS）。

---

## 0. 结论摘要

**本计划做什么**：C3 的落地（P1–P3）、**控制面收敛到无 root**（P4）、自愈改走 agent（P5）、
退役 C1 的节点 broker（P6）。

**覆盖范围**：k8s **与分离 compose 栈（含 `docker-compose.multinode.yml`，3 worker 同机）**；
**只排除 `local://` 形态**。

**已定形态（2026-09-28）**：agent = **一个 DaemonSet、两个容器**（面 A / 面 B），
用**独立镜像**（不复用 worker 镜像）；槽位身份走 **CP 下发 + CP 转发 pid**，
`worker ↔ agent` **无通道**。

**本计划明确不做什么**（都评估过、留了触发条件）：

- **不做"全链路无 uid 0"** —— 那需要把 agent 的文件面从 `chown` 换成 C2 的"以 X 创建/删除"原语。
  用户已裁定口径是「**数据面（worker + 槽位）无 root**」。替代路线记在
  `docs/c3-privilege-relocation.md` §11 第 1 项。
- **不做轴 B（沙箱树对 worker 不可读）** —— 本期只做**收敛**（把树的访问路径收到一个接口），
  不关组位。四条一致性要求见 §4。
- **不做 CP 专属 uid** —— 先取 65534（零迁移）；专属 uid 后议。
- **不支持 `local://` lane** —— C3 只覆盖分离形态，`local://` 按今天形态保留。

## 1. 数据流（实施者先看这张图）

**只有两条通道：`worker ↔ CP` 与 `CP ↔ agent`。`worker ↔ agent` 不存在**（硬规则 5）。

```
                     ┌────────────── CP（零特权，uid 65534）──────────────┐
                     │ · 唯一权威：sandbox → node / 路径 / uid / 策略      │
                     │ · 只发指令；不碰沙箱树（根挂载只读 + 8 条 RW 子路径）│
                     └───▲──────────────┬──────────────────────▲─────────┘
                         │ ① 上报       │ ② 指令（含 uid）      │ ④ 回报
                         │ ③ 报 pid     │                      │
      ┌──────────────────┴───────────┐  │      ┌───────────────┴──────────────┐
      │ worker（uid 65534，零特权二进制）│  │      │ agent（每节点，**不 fork 槽位**）│
      │  · fork 槽位                  │  └─────▶│  面 A 身份授予：65534 +       │
      │  · unshare(CLONE_NEWUSER)     │         │        cap_setuid,setgid+ep   │
      │  · 报 {pid, sandbox_id} 给 CP │         │  面 B 文件操作：root +         │
      │    （**不报 uid、不报路径**） │         │        CHOWN,DAC_OVERRIDE,    │
      │  · exec sandlock-supervise    │         │        FOWNER（同今天 broker） │
      │  · 槽位落在 worker 的 cgroup ✔│         └───────────────────────────────┘
      └──────────────┬────────────────┘
                     │ ⑤ 子进程 setresuid(X)（身份是 agent 授予的）
                     ▼
              沙箱（pool uid X）
```

**执行顺序**（详细形状见 §4 规则 5 与 Task 3）：CP 分配 uid 并发建箱指令给 worker →
worker fork+unshare 后把 `{pid, sandbox_id}` 报给 **CP** → CP 校验并**带着 uid** 转给 agent →
agent 写 `uid_map`。**worker 的上报是 fire-and-forget，不需要 CP"放行"**：
**子进程自己轮询 `setresuid(X)`**，成功即继续 `exec`。

> ⚠ **不要写成"CP 放行 worker"**（2026-09-28 改）：那会把链路变成
> **CP → worker → CP → agent → CP → worker** —— CP 在等自己发起的那次调用的回调，
> 有**连接池/死锁**风险，而且**完全没有必要**。子进程轮询即可，于是没有"放行"这个状态。

**agent 在这一步是**无状态**的**：它不持有"本节点有哪些沙箱"的表 —— uid 由 CP 在指令里给出，
agent 只负责"把它写下去"。**少一张表 = 少一个会与 CP 漂移的状态面。**

## 2. agent 的最小特权面（判定过程 + 结论）

### 2.0 部署形态（2026-09-28 定案）

| 决定 | 取值 | 说明 |
|---|---|---|
| 几个 DaemonSet | **一个**（两个容器：面 A、面 B） | 面 A 要 `hostPID`，而那是 **pod 级**字段 ⇒ **面 B 也会拿到它**。面 B 已经是 root、有 `DAC_OVERRIDE`，增量不大，但**这条要在评审里点名**，不能默默发生 |
| 镜像 | **独立镜像**（新 `deploy/docker/Dockerfile.agent`），**不复用 worker 镜像** | 于是判据"**worker 镜像里没有特权二进制**"是**可断言的硬性质**。今天"复用同一镜像"的理由是"**socket 协议是两边的契约**"（`priv-broker.yaml` 注释）—— 本计划里 `worker ↔ agent` 不再有通道，**那个理由消失** |

### 2.1 面 A（身份授予者）：非 root，65534 + 两个 file caps

**职责**：对 worker 刚 `unshare` 出来的子进程 C 写一次
`/proc/<C>/uid_map` 与 `gid_map`（内容 `X X 1`，恒等映射）。

| 项 | 取值 | 依据 |
|---|---|---|
| 运行 uid | **65534** | 实测：`map_write()` 要求 opener 在目标 ns 有 `CAP_SYS_ADMIN`，而 `cap_capable()` 的 owner 规则只把这条白给"euid == ns owner"的进程；目标 ns 的 owner 就是 worker 的 65534 |
| file caps | `cap_setuid,cap_setgid+ep` | 写任意 outside id 需要它们 |
| **容器 BND** | **必须含 `SETUID`、`SETGID`**（写 `capabilities.add`；对非 root 进程**不产生 `CapEff`**，只是把 BND 撑开） | file caps **必须是 BND 的子集**，否则连 `exec` 都 EPERM（A2-5 实测）——**与 worker 今天那两条同因** |
| 宿主的 pid 视图 | `hostPID: true`（**pod 级**字段） | 要把**容器 pid** 反查成**宿主 pid** |
| 需要的挂载 | **只有 PVC**（面 B 用）。**面 A 不需要与 worker 共享任何路径** | `worker ↔ agent` 这条通道根本不存在（硬规则 5） |

**为什么不能再小**（全部实测，2026-09-28）：

- 换成 **root 写者** ⇒ `write: EPERM`（owner 规则不触发，而它没有 `CAP_SYS_ADMIN`）；
- 加 **`SYS_PTRACE`** ⇒ 无效（**加错了能力**）；
- 加 **`SYS_ADMIN`** ⇒ 能成，但 `SYS_ADMIN` **宽得多**，**不选**（那是"用宽能力买一个本可白得的东西"）；
- 走 `path` 传输（把 token 落 argv）⇒ 那是被 transport 1 取代的形态（SL-10），**不选**。

### 2.2 面 B（文件操作）：root + 三条 cap，**与今天的 broker 逐条相同**

**职责**：建树 / 解包快照 / 写 secret / 删树 / 卷切片 / 迁移导入 / 孤儿回收。

| 项 | 取值 | 依据 |
|---|---|---|
| `runAsUser` | **0** | NFS AUTH_SYS 只认凭据 uid：`chown` 给别人 **只有 uid 0 能做**（2026-09-17 实测：65534 做 `chown 10000:65534` → EPERM；同挂载 root 成功） |
| caps | `drop: [ALL]` + `add: [CHOWN, DAC_OVERRIDE, FOWNER]`（`CapEff=0x0b`） | `CHOWN` 交属主；`DAC_OVERRIDE` 穿沙箱树的权限位（含沙箱自造的 `0700`/`0600`）；`FOWNER` 在**粘滞**目录里删别人的条目 |
| 需要的挂载 | 与今天 broker 相同的 PVC + 路径白名单**四根**（`E2B_WORKSPACE_BASE` / `E2B_STATE_BASE` / `E2B_SHARED_VOLUME_ROOT` / `E2B_IMAGE_CACHE_DIR`） | 复用 `deploy/priv/priv_common.c` 的 `realpath` + 白名单纪律，**不要新写一套** |

> **⭐ 这条最重要**：面 B 的能力集**与今天的 `e2b-priv-broker` 逐条相同** ——
> 本计划**不扩大**文件操作的能力面，只是把它**搬家**（客户从 worker 换成 CP），
> 并**新增**面 A（面 A 不是 root，是 65534 + 两个 file caps）。

### 2.3 两个面共同的红线（写进 manifest 与测试）

**禁止出现在 agent 与 worker 上**：`SYS_ADMIN`、`SYS_PTRACE`、`NET_RAW`、`privileged`、
`hostNetwork`、`allowPrivilegeEscalation: true`（**这条不是笔误**：file caps 需要
NNP=0，设了 `allowPrivilegeEscalation: false` 会让内核**直接忽略** file capabilities，
面 A 会静默失效 —— 与今天 worker 清单里那条注释同一机制）。

**唯一允许 `hostPID: true` 的是 agent 面 A**，且它**不因此获得任何能力**（只影响可见性）。

### 2.4 今天 vs 目标（能力面逐项对照）

| 组件 | 今天 | 目标 | 变化 |
|---|---|---|---|
| worker 进程 | 65534，`CapEff=0`，BND `{SETUID,SETGID}` | 65534，`CapEff=0`，**BND 可为空** | **少一个特权二进制** |
| worker 镜像特权二进制 | 2 个（`e2b-slot-spawn` 可用、`e2b-maint` 在 k8s 下**本来就 exec 不了**） | **0 个** | **−2** |
| 节点特权组件 | `e2b-priv-broker`（root，`0x0b`），客户是 worker | **agent 面 B**（root，`0x0b`），客户是 CP | **能力相同，客户换了** |
| 身份授予 | 由 worker 经 file-cap 二进制获得 | **由 agent 授予（面 A）** | **新增，且更窄** |
| CP | root | **65534** | **去 root** |

## 3. CP 收敛到无 root 的方案

### 3.1 现状（2026-09-28 实测）

CP 主容器 `uid=0(root) gid=0(root) groups=0(root),1000`；根挂载 `readOnly: true` + **8 条 RW 子路径**
（`_builds`/`_images`/`_secrets`/`_templates`/`_snapshots`/`_volumes`/`workspaces/_migrate`/`state`）；
**`workspaces/` 不在 RW 里**。盘上属主：`_images`/`_secrets`/`_snapshots`/`_templates`/`_builds`/
`state` **都已是 65534**；`_volumes` 是 `0:0 755`；`state/.uid_pool.lock` 是 `65534:65534 0600`。

### 3.2 三步，且**不需要磁盘迁移**

| 步 | 做什么 | 判据 |
|---|---|---|
| **1** | CP 主容器 `runAsUser: 65534` | 实测四条约束里三条**自动满足**：`_snapshots`/`_templates`/`_secrets`/`state` 已是 65534；`.uid_pool.lock`（0600/65534）同 uid 可开；image cache owner 是 65534 |
| **2** | **把 CP 剩下的 A 类动作交给 agent**：`_volumes` 卷根 `mkdir` + `chmod 1777`（今天 `0:0 755` ⇒ 非 root 建不了）、`_runtime/<id>` 的删除 | CP 侧 A 类**清零**；卷根建得出来、`_runtime` 删得掉 |
| **3** | `image-cache-init`（initContainer，`runAsUser: 0`）**移给 agent** | CP pod **没有 root 容器** |

**顺带必须修的独立缺陷（与上面的选择无关，今天就该修）**：
`control_plane/api/sandboxes.py::_remove_local_tree_confirming` 里，沙箱树走 broker 的**确认路径**，
而配对的那个 `_runtime/<id>` 用的是裸 `shutil.rmtree(..., ignore_errors=True)` ——
**实测在非属主下静默失败且返回真**（§13.7 / `probe_c3_a5_silent_rmtree.py`）。
CP 变 65534 之后它会从"静默失败"变成"硬失败"，**必须走 agent 的确认路径**。

**保留项（点名，不默认）**：`buildkit` sidecar 跑 uid 1000、需要 `seccompProfile: Unconfined`，
且**不能**设 `allowPrivilegeEscalation: false`（rootlesskit 的 `newuidmap` 会死）。
它**不是 root**，所以不违反口径，但它是 CP pod 里唯一保留宽 seccomp 的容器，**写进文档**。

> **★ D24 修订（2026-09-29，Task 5）——第 2 步的实现方式改了**：第 2 步的**判据**（"CP 侧 A 类
> 清零；卷根建得出来、`_runtime` 删得掉"）不变，但其中的 `_volumes` 卷根**不走 agent**：Task 5
> 的复核发现 `_volumes` 不是 CP 在那里唯一的写（`_volumes/_meta/<id>.json` 也在同一个 `0:0 755`
> 根里），于是**两条路都必须先做一次属主交棒**；交棒既然不可省，agent 路线的剩余增量就是
> **给 root 的 `e2b-maint` 加一条 `mkdir` 动词** + 给共享存储的 op 定一条节点寻址规则 —— 而
> **扩 root 文件面恰是 C3 要收的那张面**。故改走本节 ① 的第一条备选（"把 `_volumes` 迁给 CP 的
> uid 后由 CP 自己做"，§13.2/§13.5 也把它列为备选）：**`_volumes`（含 `_meta`）一次性、非递归
> 地交给 65534**，CP 保留自己的 `mkdir` + `chmod 1777` —— 交棒之后它们是**属主操作**。
> 完整理由、硬性质（非递归 / 幂等 / 有名有姓 / 校验）与**部署窗口复验程序**见
> `docs/c3-privilege-relocation.md` §13.6（裁定）与 §13.6.1（程序）。判据改写：brief 的
> "`_volumes` 的 `mkdir` 不在 CP 代码路径里" ⇒ "**CP 拥有 `_volumes`，所以它的 `mkdir`/`chmod`
> 不需要特权**"，钉在 `tests/unit/test_c3_cp_rootless.py`。

## 4. 不变量与硬规则（每个 task 的要求都隐含包含）

1. **不变量**：*谁 `fork` 槽位，谁的进程树里必须有一个能变成池 uid X 的进程。*
   ⇒ 槽位由 **worker** fork（保住 cgroup 归属），身份由**面 A** 授予。
2. **worker 必须"天生"是 65534** —— 镜像 `USER 65534:65534`，**不得**改成"以 root 启动再降权"。
   一旦经过 setuid 转换，其 `/proc/*` 归 root 所有，面 A 的写会拿到 **`EACCES`**，
   **整条链路静默失效**。（这条是面 A 的生存前提。）
3. **路径 / uid / 策略的权威来源是 CP 的记录**，不是任何请求方的自述。**worker 只报
   `{sandbox_id, action}`，绝不报路径，也绝不报 uid**（见 Task 2 的"CP 下发"形状）。
4. **agent 不把身份交给 worker 的助手**：文件操作**由 agent 自己执行**；
   身份授予**只给沙箱自己的进程**。否则"agent 发身份、worker 干活"就等于把
   `e2b-slot-spawn` 换个位置。
5. **通道方向：只有两条** —— `worker ↔ CP` 与 `CP ↔ agent`。
   **`worker ↔ agent` 不存在**（用户的硬规则）。**不得为省一跳而违反它。**
   于是槽位身份的形状是「**CP 下发** + CP 转发 pid」：

   ```
   CP    ① 分配 uid X（今天已有 allocate_host_uid，写进 record.host_uid）
   CP    ② 发建箱指令给 worker（今天已有 _provision_remote）
   worker ③ fork C → C unshare → 把 {pid, sandbox_id} 报给 **CP**      ← 只报这两个，不等回复
   CP    ④ 按硬规则 6 校验 → 指令 agent：{pid, sandbox_id, X}          ← **uid 在这里给**
   agent ⑤ 用 **NSpid 整条链**把容器 pid 反查成宿主 pid → 写 uid_map "X X 1"
   C     ⑥ 轮询 setresuid(X) 成功 → exec sandlock-supervise --uid X      ← 没有"放行"这一步
   ```

   ⚠ **反查必须带上 worker 的 pod 身份，不能只比 `NSpid`**（2026-09-28 补）：
   同一节点上可能有**多个 worker**（compose multinode 就是 3 个同机），两个 worker 各有一个
   容器 pid 42 就会撞，**而"整条 NSpid 链"也区分不了**（两个都长 `[宿主 pid, 42]`）。
   ⇒ **判据 = `NSpid` 命中 **且** 该 task 的 `/proc/<pid>/cgroup` 与**目标 worker pod 的 cgroup**
   一致**（cgroup 路径里带 pod UID，§14.2.7 实测的 `pod6d3cdd7b-…` 就是它）。
   所以 ④ 的指令里要带 **worker 的 pod 身份**，不只是 `node_id`。

   ⚠ **没有"预先推表"这一步**（2026-09-28 按读者提问合并掉）：④ 已经带着 uid 了，
   agent 不需要提前知道任何东西。**agent 因此是无状态的** —— 不持有"本节点有哪些沙箱"的表，
   于是**没有表与 CP 漂移的问题、没有 TTL、没有"先推后发"的顺序纪律**，
   每建箱也少一次 CP→agent 外呼。（早先那版把 ② 与 ④ 分成两步，② 是纯冗余。）

   **为什么这才叫"CP 下发"**：worker 的消息里**没有 uid**；uid 来自 **CP 的记录**，
   agent 只是执行者。worker 若自称别的节点的沙箱 → **agent 的表里查不到 → 拒**。

   ⚠ **worker 仍然会知道 X**（它要把 `--uid X` 交给 supervisor）—— **这无所谓**：
   身份是 agent 授予的，而 supervisor 自检 `geteuid() == X`，worker 传别的值只会让沙箱起不来。

   **代价（要认，别藏）**：**CP 进了同步路径** —— 每次槽位启动都要经它中转一次。
   增量可控（建箱本来就要经 `_provision_remote`），多的是 CP→agent 一次外呼；但**必须有超时 +
   fail-closed 点名**，否则故障会表现成"建箱莫名卡住"。
   ⚠ 而且它**与并发有关**：N 个 worker 同时建箱时，CP 侧同时有 N 条 CP→agent 外呼在飞。
   **这条要测**（判据 16），不能只写在"代价"里就算交代过 —— 已有的 `CreateQueue`
   （`E2B_CREATE_QUEUE_MAX=100` / `TIMEOUT_S=30`）管的是**准入容量**，**不管**"转发给 agent 的并发"。

   **剩下唯一要定的细节**：**pid 可能在 worker 上报与 agent 写入之间消失**（子进程崩了）⇒
   agent 的写失败必须 **fail closed 并点名**（"沙箱 S 的槽位 pid 已不在"），不许静默继续。
6. **worker→CP 的三步校验 + 一层**（`docs/c3-privilege-relocation.md` §11.1 第 9 项）：
   ① 凭据 → 身份；② 请求自称 == 凭据推出，**不一致即拒**；③ 对象用 CP 记录校验；
   ④ 源 IP 不一致**即拒**（期望 IP 取自 **k8s API**，不"从源 IP 学"）。

---

## Global Constraints

（每个 task 的要求隐含包含本节；取值**逐字**照抄，不要重新推导。）

- **JDK/语言下限**：Python 3.14；C 用 `deploy/priv/` 既有风格；Rust 改动仅限
  `third_party/sandlock`（本节计划**不改** fork 代码，除非某 task 明说）。
- **测试纪律**：覆盖行为而非实现；**断言必须精确匹配**（禁 `toContain`/`includes`/`assertContains`）；
  **禁止 SKIP 或过滤错误输出**；日志驱动排查，拒绝盲猜。
- **临时文件**：一律放仓库内 `tmp/`，**不用** `/tmp` 或 `$TMPDIR`。
- **口径**：合规口径 = 「**数据面（worker + 槽位）无 root**」。**agent 面 B 是 root 且被接受**，
  因为它**不在数据面进程树里**（不 fork、不 exec、不碰 cgroup）。
- **agent 面 A 取值**：`uid 65534` + `cap_setuid,cap_setgid+ep`。**不得**改 root、**不得**加 `SYS_ADMIN`。
- **agent 面 A 的容器 BND**：**必须含 `SETUID`、`SETGID`**（file caps 必须是 BND 子集，否则 `exec` EPERM）。
- **agent 面 B 取值**：`runAsUser: 0` + `drop:[ALL]` + `add:[CHOWN, DAC_OVERRIDE, FOWNER]`。
- **agent 部署形态**：**一个 DaemonSet、两个容器**（面 A / 面 B）；**独立镜像**，**不复用 worker 镜像**。
- **`NSpid` 反查**：必须**匹配整条链**（同一节点可能跑多个 worker），**不得只比最后一项**。
- **覆盖范围**：k8s **与分离 compose 栈（含 `docker-compose.multinode.yml`）**；**只排除 `local://`**。
  **按名字排除、且被排除者要自己声明"没有文件操作能力"（裁定 D23）**：`local://`（合体节点）、
  autoscaler 的 docker pool（`deploy/compose/docker-compose.autoscale.yml` +
  `autoscaler/backends/local.py`）、单机示例（`deploy/compose/docker-compose.yml`）——后两个在
  各自清单里显式写 `E2B_PRIV_HELPERS=off`（"从不用 broker"），钉在
  `tests/unit/test_c3_agent_manifest.py::test_the_shapes_excluded_from_c3_declare_that_they_have_no_file_ops`
  与 `tests/unit/test_worker_env_key_sets.py`。**follow-up（带触发条件）**：给 pool 配 agent
  需要先解决"CP 如何寻址一个 pooled worker 落在的**宿主**"（pool 的 worker 是 autoscaler 用
  `docker run` 起的，没有 pod/nodeName）—— 触发条件是"pool 需要 per-sandbox uid 或 route-B"。
- **CP 取值**：主容器 `runAsUser: 65534`。
- **worker 取值**：镜像 `USER 65534:65534`；目标 BND **空集**（P3 完成后）。
- **禁项**（worker / agent 面 A）：`SYS_ADMIN`、`SYS_PTRACE`、`NET_RAW`、`privileged`、
  `hostNetwork`、`allowPrivilegeEscalation: true`。**agent 面 A 唯一允许 `hostPID: true`。**
- **文档同步**：凡改 manifest 的 task，必须同步 `docs/deploy-clusters.md` 的现状节与
  `tests/unit/` 下对应的 pin（见各 task 的"钉子"）。

## File Structure

| 文件 | 职责 | 动作 |
|---|---|---|
| `deploy/priv/as_uid.c`（新） | 面 A 的实现：只做"写一份恒等 map"，**不是通用 launcher** | 新建 |
| `deploy/priv/priv_common.c` | 路径/uid 校验纪律（**复用，不重写**） | 复用 |
| `deploy/priv/priv_materialize` 族 | 面 B 的 verb 实现（`e2b-maint` 已具备 `chown`/`rm`/`walk`） | 复用 |
| `envd_service/route_b.py` | 槽位池：新增"由 agent 授予身份"的启动路径 | 改 |
| `envd_service/priv_helpers.py` | worker 侧客户端：新增 `request_identity(...)`；移除 file-cap 依赖 | 改 |
| `envd_service/uid_pool.py` | 孤儿回收改为"报告"（不再自己 chown/删） | 改 |
| `envd_service/app.py` | 启动自愈改成上报 | 改 |
| `control_plane/api/internal.py` | 三步校验 + 源 IP 层；节点 IP 从 k8s API 取 | 改 |
| `control_plane/api/sandboxes.py` | A 类动作改走 agent；A5 走确认路径 | 改 |
| `deploy/docker/Dockerfile.agent`（新） | agent 的**独立镜像**（只装 `as_uid` + `e2b-maint`，**不含沙箱运行时**） | 新建 |
| `deploy/k8s/c3-agent.yaml`（新） | agent DaemonSet：**两个容器**（面 A / 面 B），pod 级 `hostPID: true` | 新建 |
| `deploy/compose/docker-compose.*.yml` | 分离 compose 栈（含 `multinode`）加 **agent 服务**；`local://` 形态不动 | 改 |
| `deploy/k8s/worker.yaml` | 去掉 file caps 相关 BND 与特权二进制（worker 镜像不再含它们） | 改 |
| `deploy/k8s/control-plane.yaml` | 主容器 `runAsUser: 65534`；`image-cache-init` 移走 | 改 |

---

## Task 1: 面 A 原语（`as_uid`）+ 单测

**Deliverable:** 一个只能写恒等 uid/gid map 的小二进制，越界一律 fail closed 并点名。

- [ ] 写失败用例：`tests/unit/test_priv_helpers.py` 同形，断言 ① 非池内 uid 被拒；
  ② 目标 pid 的 `uid_map` 非空（已写过）被拒；③ 目标未 unshare（`uid_map` 是初始全量）被拒；
  ④ 传入 `0 X 1` 形式的"非恒等"映射被拒（面 A **只**接受恒等）。
- [ ] 跑测试确认**红**（`pytest tests/unit/test_priv_helpers.py -q`）。
- [ ] 实现 `deploy/priv/as_uid.c`：`--uid X --pid N`；写前校验上四条；成功后打印一行
  `C3-ASUID-OK pid=N uid=X`。
- [ ] 跑测试确认**绿**；`getcap` 断言 file caps 恰为 `cap_setuid,cap_setgid+ep`。
- [ ] 建**独立镜像** `deploy/docker/Dockerfile.agent`：装 `as_uid` + `e2b-maint` 到
  `/var/lib/e2b-priv/`（`0710 root:<agent-gid>`），`as_uid` 打 `cap_setuid,cap_setgid+ep`。
  **写两条 pin**：① 该目录里**恰好这两个**特权二进制，caps 与预期**逐字相等**；
  ② **worker 镜像里 `/var/lib/e2b-priv/` 不存在**（这是判据 2 的硬性质，靠独立镜像换来的）。
- [ ] Commit。

## Task 3: 槽位身份由 **CP 下发**、由 agent 授予（route B 启动路径）

**Deliverable:** worker fork 子进程、让它 unshare，**把 `{pid, sandbox_id}` 报给 CP**
（**不是报给 agent**，fire-and-forget）；CP 校验后**带着 uid 指令 agent**；agent 反查宿主 pid
并写 map；**子进程自己轮询 `setresuid`** 成功后 exec `sandlock-supervise`。
**worker 侧零特权，且不能指定 uid；agent 无状态。**

> **⚠ 验收环境（不看这条会"假通过"）**：本 task 的判据 **13**（`NSpid` + cgroup 双命中）与
> **16**（并发建箱）**都只在"同一节点上跑多个 worker"时才暴露**，而 **k8s 现在是 1 节点 1 worker** ——
> 在 k8s 上跑这两条会**全绿但什么都没测到**。⇒ **13 与 16 必须在
> `deploy/compose/docker-compose.multinode.yml`（3 个 worker 同机）上验收**；
> k8s 只跑与 worker 数无关的那几条（1 / 2 / 3 / 7）。

- [ ] 建 `deploy/k8s/c3-agent.yaml`（**一个 DaemonSet、两个容器**，pod 级 `hostPID: true`）：
  面 A 用**独立镜像**、`runAsUser: 65534`、`capabilities.add: [SETUID, SETGID]`（BND 声明）；
  面 B `runAsUser: 0` + `drop:[ALL]` + `add:[CHOWN,DAC_OVERRIDE,FOWNER]`。

- [ ] 写契约用例（`tests/contract/test_route_b_executor.py` 同形）：断言
  ① 槽位进程的**宿主 uid == X**（宿主机侧 `stat`，不是 ns 内视角）；
  ② 槽位进程的 `/proc/<pid>/cgroup` **与 worker 自己的逐字相同**；
  ③ worker 进程 `CapEff == 0`；④ worker 镜像里**没有** `e2b-slot-spawn`；
  ⑤ **worker 上报的消息里不含 uid**（用一个只接受 `{pid, sandbox_id}` 的假 **CP** 断言：
     多带一个 `uid` 字段即被拒）；
  ⑥ **worker 直接向 agent 发身份请求必须被拒**（"通道不存在" ⇒ **连接层就拒**，
     而不是靠 agent 判）—— 这是"只有两条通道"的第二个检查点；
  ⑦ **worker 的会话列表里没有指向 agent 的连接**（断言"只有两条通道"这条规则没被绕过 ——
     这是本 task 唯一一条"因为省一跳而被违反"的检查点）。
- [ ] 跑确认**红**。
- [ ] 改 `envd_service/route_b.py`：新增"identity-grant"启动路径（`E2B_SLOT_IDENTITY=agent-grant`，
  默认 `spawn` 以保回退）；`envd_service/priv_helpers.py` 加
  `request_identity(pid, sandbox_id)` —— **签名里没有 uid**。
- [ ] CP 侧新增**转发**：worker → `POST /internal/nodes/{node_id}/slot-identity`
  （body `{sandbox_id, pid}`，**没有 uid**）；CP 走硬规则 6 的三步校验，**再带着 uid 指令 agent**
  （`grant-slot`）；**worker 不直接调 agent**。
- [ ] 跑确认**绿**；再跑 `tests/security` 全档，确认没有回归。
- [ ] 实现反查时 **`NSpid` 命中 + 目标 worker pod 的 cgroup 命中**（**不能只比 `NSpid`**）；
  ④ 的指令里因此要带 **worker 的 pod 身份**。写一条用例：**同节点造两个 worker、各起一个
  容器 pid 相同的子进程**，断言 agent 只认对的那个。
  （这条在 k8s 上 1 节点 1 worker 时不暴露，**compose multinode 的 3 worker 同机会暴露**。）
- [ ] 测唯一那个细节：**pid 在 worker 上报与 agent 写入之间消失**（子进程崩了）⇒
  agent 的写失败必须 **fail closed 并点名**（"沙箱 S 的槽位 pid 已不在"），不许静默继续。
- [ ] **并发建箱（判据 16）**：N 个 worker **同时**各建 1 个沙箱（N = 该形态的 worker 数；
  compose multinode = 3、k8s = 2），重复 k 轮；断言 ① **全部成功、零 `E2B_CREATE_QUEUE_TIMEOUT_S`
  命中**；② 总耗时**不出现超线性退化**（对比"逐个建"的串行基线）；③ agent 侧日志**不出现
  "同一时刻只有一个 grant 在执行"**那种排队证据。
  **反面臂（判据非恒真）**：把 **CP→agent 客户端的连接池压到 1**，同一个用例**必须复现**
  排队或超时。**不带反面臂的这条判据等于没测。**
  ⇒ 并据此定下 **CP→agent 的并发上限配置**（≥ 该形态最大并发建箱数），写进 manifest。
- [ ] **真机**：用 `deploy/scripts/acceptance/probe_c3_userns_map_handoff.py` 的
  `--role forker/agent --pids-file` 两臂在集群上复验（判据：`C3-MAPHANDOFF-VERDICT=agent-can-map`
  **且** `C3-CGROUP=worker`）。
- [ ] Commit。

## Task 2: agent 的通道与三步校验（含 N49）

**Deliverable:** agent 与 CP 之间的**双向**认证通道（CP→agent 指令 / agent→CP 回报）；
CP 侧的身份校验按硬规则 6 落地。**agent 不持有任何授权表**（§4 规则 5 的合并说明）——
它只接受**来自 CP 的、带参数的指令**。

- [ ] 写用例（新 `tests/contract/test_internal_identity.py`）：
  ① 用 node A 的凭据请求 node B 的沙箱 ⇒ **拒**；
  ② 偷到 node B 的凭据、从 node A 的网络位置发出 ⇒ **拒**；
  ③ 同一请求，两个节点的源 IP **必须不同**（防"源 IP 层是恒真的死代码"）。
- [ ] 跑确认**红**。
- [ ] 实现：`control_plane/auth.py` 加 `node_id_for_key(...)`（key → node）；
  `control_plane/api/internal.py` 的每个 handler 走三步校验；节点 IP 从 **k8s API** 取
  （CP 已挂 ServiceAccount），**不采信 `body.get("address")`**。
- [ ] 实现 **CP → agent 的指令面**：`POST /internal/nodes/{node_id}/agent/{op}`
  （第一步只实现 `grant-slot`），**只允许写入凭据所对应的那个节点**（复用三步校验）；
  **agent 侧无状态**，不做本地授权表、不做 TTL、不做"先推后发"的顺序纪律。
- [ ] 跑确认**绿**；并加**钉子**：`tests/unit/` 下断言 worker pod 清单**不含**
  `CAP_NET_RAW`、internal API 前**没有**代理（配置层断言）。
- [ ] 真机：`kubectl` 复验两节点 worker 的请求在 CP 侧源 IP 不同（写进
  `docs/deploy-clusters.md` §现状）。
- [ ] Commit。

## Task 4: 文件操作面归 agent（worker 去特权）

**Deliverable:** 建树 / 解包 / secret / 删树 / 卷切片 / 迁移导入 全部由 agent 执行；
worker 镜像**不再含任何特权二进制**。

- [ ] 写用例：断言 worker 镜像内 `/var/lib/e2b-priv/` **不存在**（或为空）；
  断言每类操作最终都落在 agent 的路径白名单**四根**之内。
- [ ] 跑确认**红**。
- [ ] 实现：agent 面 B 复用 `e2b-maint` 的 `chown`/`rm`/`walk`（**不新写**）；
  `control_plane/api/sandboxes.py` 与 `envd_service/` 对应位点改走 agent；
  **A5 顺手修**（配对的 `_runtime/<id>` 走确认路径，不再裸 `rmtree`）。
  ⚠ **A5 的定位要读准**（2026-09-28 更正）：它所在的是 **`_destroy_local`（local lane）**，
  而两个生产栈都设 `E2B_ENABLE_LOCAL_NODE: "false"` ⇒ **A5 在生产里不可达**。
  它是**真缺陷**（静默失败 + 返回真，已实测复现），但**是死代码里的缺陷** ——
  按"正确性顺手修"对待，**不要**按"生产在漏"对待。
- [ ] **compose 分离栈也在范围内**：给 `deploy/compose/docker-compose.{prod,multinode}.yml`
  加 agent 服务（同镜像、同两个面、同 caps）；**`local://` 形态不动**。
- [ ] 跑确认**绿**；跑 `deploy/scripts/acceptance/probe_c3_a5_silent_rmtree.py`
  确认 A5 的判据从 `reproduced` 变成**不再复现**。
- [ ] 删掉 `deploy/k8s/worker.yaml` 里的 BND 两条、以及镜像里的两个特权二进制；
  同步 `tests/unit/test_worker_manifest_permissions.py` 的 pin。
- [ ] Commit。

## Task 5: CP 收敛到无 root

**Deliverable:** CP 主容器 65534、CP pod 无 root 容器、A 类动作在 CP 侧清零。

- [ ] 写用例：断言 `control-plane.yaml` 主容器 `runAsUser == 65534`、
  **pod 内没有任何 `runAsUser: 0` 的容器**、且 `_volumes` 的 `mkdir` 不在 CP 代码路径里。
- [ ] 跑确认**红**。
- [ ] 实现：主容器 `runAsUser: 65534`；`image-cache-init` 移交 agent；
  `_volumes` 卷根建立与 `_runtime` 删除改走 agent。
- [ ] 跑确认**绿**；`deployment_smoke` + `multinode_smoke`。
- [ ] 真机复验 §13.6 那张表：`_images`/`_secrets`/`_snapshots`/`_templates`/`_builds`/`state`
  在 CP=65534 后仍可写；`.uid_pool.lock` 可开。
- [ ] Commit。

## Task 6: 自愈改走 agent

**Deliverable:** 孤儿回收 = **agent 巡检 → CP 决策 → agent 执行**；worker 不再扫盘。

- [x] 写用例：① worker 崩溃且**不重启**时盘上仍在 N 分钟内收敛；
  ② CP 滚动重启期间**不误删活沙箱**；③ CP 记录**过期**时整体推迟
  （把 `protected_elsewhere` 的语义在 CP 侧重做）。
- [x] 跑确认**红**（三档门各自一条"删掉即红"的臂，见 Task 6 报告）。
- [x] 实现：agent 周期扫描 `<workspaces>/*` → 报 CP → CP 用权威记录判孤儿 → 指令 agent 删。
- [x] 跑确认**绿**；`multiworker_interference` **跑不了**（要活集群 + e2b SDK，见报告），
      改跑进程内车道（真 CP app + 真 agent app + `tmp/` 下真树）与既有孤儿契约。
- [x] Commit。

> **★ T6 裁定（2026-09-29，controller）**：1–4 项按提报的形状通过（触发/周期 30s + 120s、退避封顶
> 10min；报告 = `POST /internal/nodes/{agent_node_id}/agent/inventory`，body 只有 `{"sandboxes":[…]}`；
> 身份 = agent 自己的凭据 `E2B_C3_AGENT_TOKEN` + **主机键**寻址 + 源 IP 第二因子；三档门
> （共享记录 / `unreadable == 0` / id 条数对 `/internal/fleet/metrics`））。另加两条：**NetworkPolicy
> 的改动是刻意的、要可审**（同一提交里更新 pin `test_c3_agent_manifest.py`，注释写明 agent 为什么
> 需要出口，并保持"没有别的入口"不变）；`multiworker_interference.py` 需要活集群是**可接受的缺口**，
> 说清楚并改跑进程内车道，不伪造集群运行。**worker 键的寻址路径（grant-slot / file op）不得被削弱**
> —— 新增的是 `resolve_host`，它读 agent pod 自己，`resolve` 一字未动（`test_c3_agent_client.py` 的
> 既有 pin 原样通过）。

## Task 7: 退役 C1 与现场清理

**Deliverable:** C1 的 `e2b-priv-broker` DaemonSet 与 socket transport 退役；
盘上的历史残留清掉。

- [ ] 清 **N48**：5 棵属主 0 的老树（含 2 棵 `0777`）—— **删前逐棵 `stat` 留证**，
  删后断言 `workspaces/` 下不再有 `owner=0` 条目。
- [ ] 删 `deploy/k8s/priv-broker.yaml`、`E2B_PRIV_HELPER_TRANSPORT=socket` 分支与
  `E2B_BROKER_PEER_UID/GID` 的三处同源断言。
- [ ] 加**钉子**：断言清单里不再有 `e2b-priv-broker`；断言 worker 与 agent 面 A 的
  **禁项**（§2.3）一个都不在。
- [ ] `docs/deploy-clusters.md` 现状节、`docs/production-deployment-requirements.md` §5.4(b)、
  `README.md` 的 env 表同步。
- [ ] Commit。

---

## 验收矩阵（哪条判据、在哪测）

| # | 判据 | 在哪测 | 出处 |
|---|---|---|---|
| 1 | 槽位宿主 uid == X，且 `/proc/<pid>/cgroup` 与 worker 逐字相同 | 真机（Task 3） | §14.2.7 已预演 |
| 2 | worker `CapEff=0` 且镜像里**没有**特权二进制 | 单测 pin（Task 4 片 B 已落：`test_c3_agent_manifest.py::test_the_worker_image_has_no_privileged_binary_and_the_agent_image_has_both`）+ 真机 | §2.4 |
| 3 | agent 面 A 的 caps **恰为** `cap_setuid,cap_setgid+ep`，uid 65534 | 单测（`getcap` + `stat`） | §2.1 |
| 4 | agent 面 B 的 caps **恰为** `0x0b` | 真机 `grep CapEff` | §2.2 |
| 5 | 禁项（`SYS_ADMIN`/`SYS_PTRACE`/`NET_RAW`/`privileged`）不在 agent 与 worker 上 | 单测 pin | §2.3 |
| 6 | 三步校验：① 跨节点凭据 ⇒ 拒；② 偷凭据 + 异地源 IP ⇒ 拒；③ 两节点源 IP 不同 | 契约 + 真机（Task 2） | §11.1 第 9 项 |
| 7 | A5：非属主下不再"静默失败且返回真"（**local lane 内的正确性问题**） | 探针（Task 4） | §13.7 |
| 8 | CP 主容器 65534、pod 内无 root 容器 | 单测 + 真机（Task 5） | §3 |
| 9 | worker 崩溃不重启时盘上仍在 N 分钟内收敛 | 真机（Task 6） | §11.1 第 5 项 |
| 10 | `workspaces/` 下无 `owner=0` 残留 | 真机（Task 7） | N48 |
| 11 | **不得设为** `allowPrivilegeEscalation: false`（面 A 会静默失效） | 单测 pin | §2.3 |
| 12 | **agent 面 A 的容器 BND 含 `SETUID`/`SETGID`**（否则 file caps 连 exec 都 EPERM） | 单测 pin | §2.1 |
| 13 | **反查 = `NSpid` 命中 + worker pod 的 cgroup 命中**（两者都要）：同节点两个 worker 各有一个容器 pid 相同的子进程时，只认对的那个 | 单测 + 真机（**compose multinode**，Task 3） | §1 的 ⚠ |
| 14 | **compose 分离栈（含 multinode）里有 agent 服务**，且 `local://` 形态未被改动 | 清单解析 pin | Global Constraints |
| 15 | agent 用**独立镜像**：`Dockerfile.agent` 存在，且 **worker 镜像里没有 `/var/lib/e2b-priv/`** | 单测 pin（Task 4 片 B 已落：`test_c3_agent_manifest.py::test_the_worker_image_has_no_privileged_binary_and_the_agent_image_has_both`） | §2.0 |
| 16 | **并发建箱不因 CP 中转而串行化**：N 个 worker 同时建箱 ⇒ 全部成功、零队列超时、无超线性退化；**反面臂**（CP→agent 池压到 1）必须复现排队 | 真机（**compose multinode**，Task 3） | §4 规则 5 的代价 |

> **⚠ 判据 13 与 16 的验收环境必须是"同机多 worker"**（`docker-compose.multinode.yml`，3 个 worker）。
> **k8s 现在是 1 节点 1 worker**，这两条在 k8s 上会**全绿但什么都没测到**（假通过）。
> 其余判据与 worker 数无关，k8s 即可。

## 回退

- **每个 task 都有开关**：Task 2 的 `E2B_SLOT_IDENTITY=spawn|agent-grant`（默认 `spawn` 保回退）；
  Task 4/5 的 A 类委托同理。
- **C3 双向可回退**：新树由 worker 建成、老代码（C1 的 broker）仍能 `chown` 接管 ——
  所以 P1–P5 期间两条路可以并存，直到 Task 7 才拆桥。
- **顺序**：Task 7（拆桥）**必须**在所有真机验收通过之后。

**⚠ Task 7 已执行（2026-09-29）—— 上面第二条的那半句话到此为止。** 桥拆了：`e2b-priv-broker`
DaemonSet、`E2B_PRIV_HELPER_SOCKET`、worker 的 `wait-for-broker` 闸门与 `socket` transport 全部退役
（`E2B_PRIV_HELPER_TRANSPORT=socket` 现在是启动期**具名拒绝**）。现行的回退面只有两条**单点**开关，
再往前的形状都要整批 revert：

- `E2B_SLOT_IDENTITY=spawn|agent-grant`（Task 2）—— **仍然可原地切**（agent 还得在，文件操作还走它）；
- `E2B_PRIV_HELPER_TRANSPORT=agent|exec`（Task 4）—— 可切，但 `exec` **要求镜像里那两个
  file-capability 二进制还在**（出厂 worker 镜像已不含它们）⇒ 只改 env 不改镜像 = 启动自检具名拒绝；
- 退出到 **C1 的 broker 形状 / root worker**：**整批 revert 清单 + 镜像**，没有单开关。

盘上数据不受影响（树仍是 `0770 owner=<池 uid> group=<worker gid>`，任何 root 进程都能接管）。
完整口径见 `docs/c3-privilege-relocation.md` §14.8 与 `docs/k8s-deployment.md` §24.2。

## Self-Review

- **口径一致**：全篇按「数据面 worker 无 root」；agent 面 B 的 root **明文记录并被接受**，
  且理由是"它不在数据面进程树里"（§3 / Global Constraints）。
- **不扩面**：面 B 与今天 broker 的能力集**逐条相同**（§2.2 的 ⭐）；没有引入 `SYS_ADMIN`、
  `SYS_PTRACE`、`privileged` 中的任何一个。
- **可证伪**：11 条判据都能在单测或真机上翻转；其中第 6 条的 ③ 是专门为"防死代码"设的。
- **未做但已记账**：轴 B（四条一致性要求）、全链路无 uid 0（替代路线）、CP 专属 uid、
  `local://`、N47/N48/N49 三条 open-issues。
- **本次自查改掉的四处**（2026-09-28，读者提问 + 自查）：① 去掉"预先推授权表"那一步
  ⇒ **agent 无状态**（没有表、没有 TTL、没有"先推后发"的顺序纪律、每建箱少一次外呼）；
  ② 去掉"CP 放行 worker"⇒ **没有嵌套回调**，子进程轮询即可；
  ③ 补 **`NSpid` 整条链**匹配（同节点多 worker 会撞）；
  ④ 补 **面 A 的容器 BND**（file caps 是 BND 子集才 exec 得动）。
- **一处定位更正**：**A5 在 k8s 生产里不可达**（它在 `_destroy_local`，而两个生产栈
  `E2B_ENABLE_LOCAL_NODE: "false"`）。它是真缺陷，但**是死代码里的缺陷** ——
  按"正确性顺手修"对待（Task 4），**不要按"生产在漏"对待**。
- **依赖**：**Task 2 先于 Task 3**（Task 3 的转发要走 Task 2 那条已认证的 CP→agent 指令面）；
  **Task 2 先于 Task 4**（否则 CP 会"据自陈身份"指挥 agent 干活）。
- **⚠ 验收环境**：判据 **13**（双命中反查）与 **16**（并发建箱）**必须在
  `docker-compose.multinode.yml`（3 worker 同机）上验收** ——
  在 k8s（1 节点 1 worker）上它们会**全绿但什么都没测到**。其余判据与 worker 数无关。
- **worker 说不出一个 uid**：Task 3 的上报形状是 `{pid, sandbox_id}`，**没有 uid 字段**
  （判据 ⑤ 用一个"多带 `uid` 就拒"的假 agent 钉住它）。

## 参考

- 设计与全部实测：`docs/c3-privilege-relocation.md`（§11.1 决策书、§11.2 agent 职责边界、
  §14.2.7 (d) 的端到端实测与内核机理、§13 CP 特权动作清点）。
- 被替代的路线：`docs/c2-ownership-frontload.md`（C2）、
  `docs/superpowers/plans/2026-09-27-priv-broker-externalization.md`（C1，本计划要退役的）。
- 探针：`deploy/scripts/acceptance/probe_c3_userns_map_handoff.py`（含 `--role forker/agent/matrix`
  与 `--pids-file` 的生产级 rendezvous 臂）、`probe_c3_a5_silent_rmtree.py`、
  `probe_broker_authorization_surface.py`；Job pod `deploy/k8s-k0s/c3map-probe-agent.yaml`。
- 待办：`docs/open-issues.md` 的 **N47**（broker 授权面）、**N48**（属主 0 老树）、
  **N49**（内部 API 自陈身份）。
