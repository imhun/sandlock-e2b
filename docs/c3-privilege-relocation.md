# C3 三角分工（控制面零特权 / agent 执行 / worker 跑沙箱）设计评估 —— **已实施（Task 1–7 上线）**

> **状态：设计已选（2026-09-28）；实现见 Task 1–7 的落地记录（§11.2.1、§14、Task 6 的验收小节），
> 已于 2026-09-29 在 k0s 集群上线（`docs/deploy-clusters.md` §7.9）。** 下面标着"未实施""本次不做"
> 的段落是**设计当时的原话**，保留为历史；现行口径以上线记录为准。
> **✅ 已裁定：采用 C3，agent 作为特权操作组件；合规口径 = 「数据面（worker + 槽位）无 root」。**
> ⚠ 口径要读准 —— **agent 的文件操作面仍然是 `euid 0`**（NFS 上 `CAP_CHOWN` 不过网，§6）；
> 买到的是"**uid 0 不在数据面的进程树里**"。更强口径（全链路无 uid 0）的可行路线已评估、
> 记在 §11 第 1 条，**本次不做**；而按"缩小能力面"看，C1 反而更优（§7.7）。
> 本文是 `docs/c2-ownership-frontload.md`（C2）的**替代路线**，不是它的加强版：C3 不做
> `e2b-as-uid` 那 4–5 个动词，而是把建箱/拆树/记账整体**上移到一个独立的特权 agent**，
> 控制面只发指令、不再持有特权。目标是读者提出的那句：
> 「**worker 只负责执行沙箱的代码，不做任何特权操作**」。
>
> **先说结论**：分工是「**控制面只发指令（零特权）→ 特权 agent 执行操作 → worker 只跑沙箱**」。
> worker 能否真的做到零特权**已经实测答完**（§14.2.7）：**能**，而且**进程树与 cgroup 仍然留在
> worker**——办法是 worker 自己 fork，由一个"与它同 uid（65534）、只握 `SETUID`/`SETGID`"的
> agent 代写一次 `/proc/<C>/uid_map`。它要求 agent 与 worker **同节点**（靠 `hostPID` 看见对方
> 的 pid），所以取舍被压向 §3.2 的"每节点 agent"。
> 另外，读者目标里的后半句「把最危险的沙箱内部操作完全隔离」**不是同一件事**，
> C3 单独做不到，必须另立一刀（§6）。

## 0. 两条轴，别混在一起

读者目标里其实压着两条互不相干的安全轴。把它们分开，后面所有取舍才谈得清：

| | 轴 A —— 特权面 | 轴 B —— 数据可达性 |
|---|---|---|
| 问题 | 谁能**改变属主**、谁能**成为别人** | 谁能**读到沙箱树里的字节** |
| 今天的持有者 | 节点上的 root broker（chown/rm/walk） | **worker**（靠 `0770` 的组位） + 池 uid 自己 |
| C1 的结果 | 收敛到一个 3 动词的白名单 daemon | 不动 |
| C2 的结果 | 收敛到一个 4–5 动词的 setuid 原语 | 不动 |
| **C3 的结果** | **收敛到 worker 的 1 个动词** | **不动 —— 除非单独做 §6** |

**C3 只动轴 A。** 若不做 §6，今天 worker 照样能读所有租户树里的每一个字节，"隔离沙箱内部
操作"这个目标**不算达成**。这一条必须先说破，否则会得到一个自我感觉良好但没解决问题的方案。

## 1. 现状：特权动作的发起者是 worker，不是控制面

先把链路钉死（这与 @读者的直觉相反）：

```
CP API → 选节点 → HTTP 调 envd (_provision_remote)
                          ↓
                  worker 侧 envd 干活          ← 执行者
                          ↓
                   worker 调 broker (chown/rm/walk)
```

- 控制面只**触发**（`control_plane/api/sandboxes.py::_provision_remote` 把请求 HTTP 推给 envd）。
- 真正 `mkdir`/`copytree`/`chown` 的是 worker 进程里的 `envd_service/agent.py`。
- broker 的 peer 门是 `E2B_BROKER_PEER_UID=65534`，**root 连不上**（2026-09-27 上线实测：
  探针以容器 root 连业务 socket，被 `peer uid 0 does not match E2B_BROKER_PEER_UID=65534`
  拒掉，容器 `Running 0/1` 反复重启、rollout 超时；后来专门为探针开了容器私有的
  `health.sock` 才绕过去）。C1 的全部设计意图就是**让 worker 能完成特权动作**。
- 还有一批路径 worker **完全自主**触发，控制面根本不在链路上：
  `_startup_uid_reconcile` / `_startup_reconcile_once`（worker 启动时扫盘回收孤儿）、
  `checkpoint_store` 镜像目录回收、`route_b.py` 策略文档、`volumes.py` 卷切片建立/删除。

所以「这些特权操作应该由控制面触发」**今天不是事实**；它是 C3 要达成的**目标**。

## 2. 槽位归属：它今天在 worker 里，但**可以搬走**（有代价）

`envd_service/priv_helpers.py::Helpers.slot_spawner` 用
`pass_fds=(control_fd, events_fd)` 把两个描述符**穿过 execve** 交给 `sandlock-supervise`：

```python
supervise_args += ["--events-fd", str(events_fd)]
fd_list.append(events_fd)
...
return subprocess.Popen(argv, ..., pass_fds=tuple(fd_list))
```

代码注释原文：

> *a second descriptor, kept open across the broker's execve, so the slot sees the same
> number the worker wrote into its own fdinfo.*

**fd 只能在 fork 出来的后代里继承。** 这是"启动器必须被 worker 自己 exec"的最硬的一条理由 ——
今天 `e2b-slot-spawn` 之所以能做 `setuid`，恰恰因为它是**被 worker 自己 exec 的**：它仍在
worker 的进程树里，worker 的 fd 穿过这次 execve 原样到达槽位。

### 2.1 查清并**推翻**的一条：namespace 不是依赖

本文初稿把"槽位必须落在 worker 的 namespace 树里"列成了第二条理由。**这条是错的**，已核源码：

- `sandlock-core/src/context.rs`：槽位**自己** `unshare(CLONE_NEWUSER)` 并写 uid/gid 映射
  （已是目标 uid 时走 `0 -> euid` 的 self-map，**不需要特权**）；`pid_ns` 是**策略选项**，
  由 supervisor 自己 `CLONE_NEWPID`；netns 也在它自己的 userns 里建
  （`unshare(CLONE_NEWNET)` 靠 userns 内的 `CAP_SYS_ADMIN`）。
- `sandlock-supervise/src/main.rs` 模块注释写明它**不携带任何 capability**，
  「**the launcher is responsible for dropping privileges**」—— 它假设自己被启动时**已经是 uid X**。

⇒ 槽位是一个**自足的名字空间树**，不需要继承 worker 的任何 ns。**启动器不必是 worker 自己。**

### 2.2 唯一真实依赖：**谁当父进程**

worker 不只是"起"槽位，它还**管**槽位，而这一段是彻底的父子进程语义
（`envd_service/route_b.py` 的 `W1SlotPool`）：

| 用途 | 调用 | 为什么绑父进程 |
|---|---|---|
| 存活判定 | `handle.process.poll()`（`slot_dead()`、启动等待环、`request()` 前置检查） | `poll()` = `waitpid(pid, WNOHANG)`，只对自己的子进程有效 |
| 优雅关停 | `handle.process.wait(20)` | 同上 |
| 兜底强杀 | `handle.process.kill()` → `wait(10)` | 槽位跑在 uid X、worker 是 65534；代码里甚至有一条"**survived SIGKILL**"的实测分支 |

注意这只是**兜底**：正常关停走控制通道上的 `kill_child` verb（`SlotChannel.kill()` 的注释：
*"The in-process `ExecProcess.kill()` is SIGKILL-only; over the slot `kill_child` carries the
signal number"*），由 supervisor 去杀它自己的孩子。

### 2.3 于是"槽位搬走"有两档

| 搬到哪 | 结论 | 需要什么 |
|---|---|---|
| **控制面** | ✘ 不成立 | fd 过不了节点边界（`SCM_RIGHTS` 只在**同机** unix socket 上有效） |
| **每节点 agent** | ✔ 成立，**但要重做存活/回收协议** | ① fd 经**同机 unix socket** 用 `SCM_RIGHTS` 交出去 —— **由 agent 发起连接、worker 在同一条连接上回发**（worker 不得发起，见 §14.2.3）；② 存活/退出改由 agent 报告（传 pidfd 只能知道**死没死** —— `waitid(P_PIDFD)` 对非子进程返回 `ECHILD`，**退出状态拿不到**）；③ 兜底强杀归 agent |

⇒ 「**worker 零特权操作**」**不再是不可达**，它取决于 agent 放在哪一层 —— 见 §3.2，
而不变量本身写在 §14.2.2。

## 3. C3 的定义：三个角色

### 3.1 分工

| 角色 | 职责 | 特权 | 今天的对应 |
|---|---|---|---|
| **控制面** | **只发指令**：校验、授权、排程、记账、下命令 | **零**（本期收窄到无特权） | `control-plane` pod —— **今天以 root 跑**（§4.2） |
| **特权 agent** | **执行操作**：建树/解包/交属主/删树/遍历/写 secret | **全部集中在这里** | 节点级 `e2b-priv-broker` DaemonSet，**但它的客户是 worker，不是 CP** |
| **worker** | **只跑沙箱**：envd API、槽位生命周期、（可选）起槽位 | 目标 0（或 1，见 §3.2） | worker StatefulSet（65534，BND = `{SETUID, SETGID}`） |

这个三角最大的价值，是它**回答了本文初稿留的那个开放问题「CP 的 root 收不收窄」** —— 收窄，
而且把特权换成一个**可以单独审、单独测、单独限流、单独换**的组件。**CP 从此不进特权路径。**

> **2026-09-28 收紧**：agent 的定位从"**执行**特权操作"进一步收紧成
> 「**身份的唯一授予者**」—— worker 可以请求身份、不能自己获得身份；而**文件操作由 agent
> 自己执行，不把身份交给 worker 的助手**。理由与那条必守的分界见 §11.2。

与 C1/C2 的对照：

| | **C1**（已上线） | **C2** | **C3**（本文） |
|---|---|---|---|
| 谁发指令 | CP → worker | CP → worker | **CP → agent** |
| 谁执行特权动作 | worker（经节点 broker） | worker（经 setuid 原语） | **agent** |
| worker 侧特权动作类别 | 4 类 | 5 类 | **0 类（或 1 类，见 §3.2）** |
| CP 的特权 | root（既成事实） | root | **无** |
| 特权组件 | 每节点 1 个 root daemon | 无 uid 0，但 worker 侧 3 个 file-cap 二进制 | **agent 一个组件** |
| 节点级常驻 root | 有 | 无 | 取决于 agent 形态 |
| 轴 B（沙箱树可读性） | worker 组位可读 | worker 组位可读 | **不变（要 §6 才动）** |

### 3.2 关键决策：agent 放在**每节点**还是**每集群**

这一条决定 worker 能不能真做到零特权，是全案最重要的一格。

| | **A. 每集群一个 agent**（Deployment，挂同一份 PVC） | **B. 每节点一个 agent**（DaemonSet） |
|---|---|---|
| 文件操作 | ✔ 够用 —— 今天 CP 就是这么干的（§4.2） | ✔ 也够用 |
| 槽位启动 | ✘ 跨不了节点（fd 过不去，§2.3） | ✔ 同机 socket + `SCM_RIGHTS` 反向交付（§14.2.3） |
| **worker 特权面** | **1 个动词**（还得自己起槽位） | **0 个**（§14.2.7 的 uid_map 代写；⭐ **且不必重做存活/回收协议**） |
| 与 worker 的信任边界 | 无（worker 根本连不上 agent） | **多一条入站**：**agent → worker**，worker 只应答、不发起（§14.2.3） |
| 复杂度 | 低 | 高（进程树、存活协议、单点） |
| 故障影响面 | CP 挂，agent 还在 | agent 挂，**该节点所有沙箱**受影响 |

⇒ 两条都真实可选，**不是"哪个更好"，而是"愿意为 worker 的零特权付多少代价"**：

- **选 A**：worker 保留 §4.3 的那个动词（"以池 uid 起 supervisor"），但特权的**决策权**被拿走 ——
  worker 只能"请求启动一个已存在的定义"。这是零特权的**实质近似**（它已经不掌握任何可自由
  组合的特权参数）。
- **选 B**：worker 真正零特权。**两条实现路，代价差一个量级**：
  - **B1（2026-09-28 实测，优先）**：worker **仍然自己 fork** 槽位，agent 只替它**写一次
    `/proc/<C>/uid_map`**（§14.2.7）。进程树与 cgroup 留在 worker ⇒ **`W1SlotPool` 一行不用改**，
    `poll()`/`wait()`/`kill()`/`stderr`/`pid` 全部照旧。代价只有"agent 要多一个以 65534 跑、
    只握 `SETUID`/`SETGID` 的面"。
  - **B2（备选）**：agent 起父进程，worker 交 fd（§14.2.3）—— 那才需要把 `poll()`/`wait()`/
    `kill()` 那套整体重做（pidfd 只能知道死没死、拿不到退出状态；stderr 与兜底强杀都要经 agent 回传）。

⚠ 但 **B1 要求 agent 与 worker 同节点**（它靠 `hostPID` 看见对方的 pid）⇒ **B1 只在每节点形态下成立**，
这反而把 §3.2 的取舍压向了 B。

**建议先按 A 落**，把 §4.3 做完：它已经拿到零特权里最值钱的那部分（**决策权**），
而代价只有"worker 仍 exec 一个钉死的启动器"。**B 单独立项** —— 它动的是 `W1SlotPool` 这个成熟
组件，风险与收益不成比例地集中在那一处。

> ⚠ **2026-09-28 更正（§14.2）**：本节初稿写「B 形态与"worker 不能直连 agent"冲突」——**那是错的**。
> 规则 ④ 的正确读法是「**不能由 worker 发起连接**」，而 fd 传递**不看发起方向**（`SCM_RIGHTS`
> 只要两端有一条已建立的 socket）。**agent 拨 worker、worker 在同一条已接受的连接上回发
> `control_fd`/`events_fd`** —— worker 全程没有发起过连接 ⇒ **B 形态没有被规则排除**，
> 它是"worker 真正零特权"的唯一现实路径。
> ⚠ **也不要用 `path` 传输去替代**：那是 transport 1 被造出来**取代**的形态（token 落在 argv、
> `/proc/<pid>/cmdline` 0444 可读），见 §14.2.6。

**一句话**：C3 = 控制面零特权、agent 独占特权、worker 只跑沙箱。

## 4. 位点重分配

### 4.1 归属表（把 C2 文档 §5 的 10 条按新目标重排）

| # | 位点 | C3 归属 | 依据 |
|---|---|---|---|
| 1 | 建树（`agent.py` 的 `mkdir`/`copytree` 快照/`<ws>/workspace`） | **CP** | CP 已挂同一份 PVC、已是 root、已有 `_provision_local` 同形代码 |
| 2 | 属主交棒（`uid_pool.apply_sandbox_ownership`） | **CP**（或以 X 建立后直接消失） | |
| 3 | 卷切片（`volumes.py`） | **CP** | CP **今天就已经在建卷根**（`control_plane/api/volumes.py`） |
| 4 | 检查点镜像目录（`checkpoint_store`） | 拆两半：**建**归 CP，**GC** 归 CP 巡检 | |
| 5 | route-B 策略文档（`route_b.py`） | **CP**（见 §4.3 —— 这一条比看上去重要） | |
| 6 | 沙箱 secret 注入（`executors/sandlock.py`） | **CP** | CP 已经管 `_secrets` |
| 7 | 孤儿回收（`uid_pool.reconcile`） | **CP**（须重新设计，见 §5.2） | |
| 8 | 迁移导入 tar 解包 | **CP** | |
| 9 | 平台态属主 | 已完成（C1 wave 2） | |
| 10 | 树根可写位（`1777` / `3777`） | **CP 的 init**（今天是 worker-init） | |
| — | **起槽位（`setuid` → supervisor）** | **worker（A 形态）或 agent（B 形态）** | 见 §2.2 与 §3.2 |

### 4.2 为什么这条路不是"新发明"

控制面**已经**在管共享 PVC 的一半，这不是假设：

- Pod 级 `securityContext` 只有 `fsGroup: 1000`，主容器没有 `runAsUser`；manifest 自己的注释
  就写着 *"the root control-plane container"* ⇒ **CP 主容器以 root 跑**。
- `sandbox-shared` 以 RW 挂到同一个 `/var/lib/e2b-sandboxes`，逐个子挂载
  （`_builds`/`_images`/`_secrets`/`_templates`/`_snapshots`/`_volumes`/`workspaces/_migrate`/`state`）。
- `control_plane/app.py` 建 `workspace_base`；`control_plane/api/volumes.py` 建/删卷根与切片、
  `shutil.rmtree`；`control_plane/registry/{snapshots,secrets,templates,volumes}.py` 建各自的根。

所以 C3 与其说是"引入新架构"，不如说是**把一条已经存在的分工线画完整**。

### 4.3 一个便宜的加强：把特权决策从 worker 的 argv 里拿走

今天 `e2b-slot-spawn` 的调用形如
`spawn --uid X --gid X -- <sandlock-supervise 绝对路径> <args>` ——
**uid 由 worker 的 argv 决定**（program 已被 `validate_spawn_program` 钉死为 supervisor）。

如果 §4.1 第 5 条（策略文档）也归 CP，那么可以得到一个更强的形态：

> 策略文档由 **CP 写进一个 root 属主、worker 不可写的目录**；worker 只能请求
> "启动一个**已存在**的槽位定义"，uid 从策略源读，不从 argv 读。

这样 worker 对特权决策的**影响力**也归零 —— 它剩下的只有"对一组预先存在的定义做选择"。
这条比"缩小 verb 数量"更值钱，而且成本很低。**但它依赖 §4.1 第 5 条真的搬得动**，
所以列成一个独立的、可验收的子项，不要与主语混在一起。

## 5. 需要新增/改造的组件

### 5.1 特权 agent 组件

**这个组件的存在本身就是"CP 的 root 收窄"的实现方式**：CP 不进特权路径，agent 独占特权。

- **接口要窄**：动词白名单（`mkdir`/`write`/`extract`/`chown`/`rm`/`walk` 按需取子集）+
  `realpath` + 路径白名单 + uid 必须落在池内。
- **纪律复用，不要新写一套**：`deploy/priv/priv_common.c` 已经有 `realpath` + 四根白名单 +
  `FTS_PHYSICAL` + `lchown`/`unlinkat`（符号链接永不跟随），并且 `deploy/priv/maint.c` 的 usage
  头已经把 `chown`/`rm`/`walk`/`serve`/`ping` 五种形态写全 —— **直连 CLI 形态就是 agent 要用的
  形态**。已有同形单测 `tests/unit/test_priv_helpers.py`。
- **鉴权**：CP → agent 用 token / mTLS。仓库里有现成形态：`deploy/quota_agent/` 的
  `E2B_QUOTA_AGENT_TOKEN`，调用侧一个 URL 开关（`E2B_QUOTA_AGENT_URL`）就把调用切过去。
  **但要读它的教训**：quota-agent 在 compose lane 部署了，**k8s manifest set 里一个都没有**，
  于是 k8s 默认落在 degraded 形态。C3 走同一条路，必须**一开始就决定 k8s lane 要不要它**，
  而不是留成"默认降级"。
- **多副本协调**（若选 §3.2 的 A 形态）：接进 `docs/control-plane-multi-replica.md` 的既有框架
  （Redis `RedisNodeStore` + 共享 base 上的 `flock`；NAS 只有 `vers=4.0` 跨节点真互斥）。
- **绝不落在事件循环上**：`control_plane/api/volumes.py` 那条注释
  （*"N32: a NAS `rmtree` in an async handler is a stall, and a stall is what the ..."*）
  对所有新动词同样适用。
- **它会是系统里价值最高的单一目标**：见 §7.8。

### 5.2 自愈的重新设计 —— 这是最大的实质改动

今天的自愈是 **worker 启动时扫盘**：`_startup_uid_reconcile`（回收孤儿 uid + 把 stale 树
chown 回 worker，`uid_pool.py::_reconcile_locked`）、`_startup_reconcile_once`（xfs 配额
孤儿树 GC）。worker 一启动就收敛，不依赖任何外部组件。

C3 之后 worker 不再有权扫盘/回收，必须三选一：

| 选项 | 形态 | 代价 |
|---|---|---|
| a | CP 侧周期巡检（需要 fleet 视图） | CP 不可用期间盘上不收敛；要接多副本协调 |
| b | worker **只报告**，CP 决策并执行 | 多一次往返；报告本身不权威（worker 没权限 stat 别人的树？—— 见 §6） |
| c | 给 worker 一个极窄的"只能删自己名下的树"的能力 | 把删除拉回 worker，与 C3 目标相抵 |

**推荐 a + b 混合**：CP 巡检是权威，worker 的报告只是线索。**不要选 c** —— 它会让 §3 表里
"worker 侧特权动作 = 1 类"变成 2 类，而且重新引入"worker 能删树"这个正是要消掉的东西。

⚠ 有一个连带效应必须提前想清楚：今天 worker 的孤儿判定靠 `_uid_pool.lock` 的 flock +
「属主 ∈ 池 且 无记录」这个谓词。搬到 CP 之后这个谓词要在**没有 worker 的组位视野**下重算，
否则会出现"CP 看不到某棵树 ⇒ 判为无主 ⇒ 删掉活沙箱"这类事故。今天 worker 里已经有
`protected_elsewhere` 这类保护（`worker.yaml` 注释记着一次实测：重启的 worker 把四棵活树
都看成无主，全靠这层保护才没删）。

## 6. 轴 B：真正的"隔离沙箱内部操作"要另做一刀

即使 C3 全做完，**worker 仍然能读沙箱树里的每一个字节**（`0770` 的组位，见 C2 文档 §6 H3）。
要关掉这一格，只有 C2 文档 §4.2 的**选项 3**：放弃组位模型，树对 worker 完全不可读
（`0770` → `0700`），所有访问"以 X"。

**C3 让这件事第一次变得可行**，这是 C3 相对 C2 的最实质好处：今天 worker 之所以需要组位，
是因为 walk/记账/删内部文件都在 worker 手上；这些上移到 CP 之后，worker 对树的**唯一**剩余需求
就是"以 X 服务沙箱自己的 API 调用"（租户文件读写早就走 `SandboxWriter`，即以沙箱身份执行）。

代价必须一起接受：

- worker 侧所有 `walk`/`stat`/记账变成跨进程调用；
- `0770` → `0700` 会动到一批既有断言与测试（C2 文档 §8 第 4 条已经在说测试矩阵变大）；
- **CP 成为系统里唯一能读全部租户数据的地方** —— 它从"控制面"升级为**价值最高的单一目标**。
  这一条必须写进威胁模型，而不是当作没发生。

**建议把 §6 单独立项**，不要塞进 C3 的第一期。它是"目标的后半句"的唯一实现路径，
但它和"特权上移"的风险性质完全不同（一个是特权集中，一个是数据集中）。

## 7. 代价与风险（逐条可验收）

1. **CP 成为全局热路径**：建箱、拆箱、记账都依赖 CP。CP 不可用 = 不能建、不能删、
   磁盘不能回收。今天 worker 本地就能拆箱，C3 之后不行。
2. **延迟**：NFS 上的建树/解包从"worker 本地"变成"CP 经网络"。既有教训直接适用 ——
   `control_plane/api/volumes.py` 的注释记着「N32: a NAS `rmtree` in an async handler is a
   stall, and a stall is what the ...」⇒ 所有新的特权动作都必须在 `asyncio.to_thread` 之类
   的线程里，不能落在事件循环上。
3. **原子性/竞态**：今天建树与起槽位在**同一个进程**里，共享本地锁。拆开之后需要跨服务协调，
   且必须让 C2 文档 §10 的第一条不变量（**同一个沙箱必须复用同一个池 uid**）跨服务成立。
   这是最容易出隐蔽 bug 的一格，要有崩溃点用例。
4. ~~CP 的 root 面变大~~ → **已定：CP 收窄，特权归 agent**（§3.1）。
   代价随之转移：特权不再是"集中在 CP"，而是"集中在 agent"；CP 从"有特权"变成"只发指令"。
   ⚠ **但要真做到 CP 零特权，得先把 CP 今天用 root 做的那些事逐条找出来** —— 见 P0。
5. **`local://` 形态**：`node.address == "local://"` 时 CP 与 worker 同进程
   （`_provision_local`），C3 的边界在这条 lane 上**不成立**。要么单独定义，要么明确声明
   该 lane 不在 C3 覆盖范围。
6. **双轨期**：迁移期两种形状并存 ⇒ 需要"这棵树是 C2/C3 形状还是老形状"的可判定标记，
   否则"树必须出生即正确"不可验证。
7. **合规口径 → ✅ 已裁定（2026-09-28，用户）：「数据面（worker + 槽位）无 root」**
   （§11 第 1 条）。**但这个口径要读准**：C3 **不消除** uid 0 —— **agent 的文件操作面就是
   `euid 0`**（NFS 上 `CAP_CHOWN` 不过网，§6）。它买到的是「**uid 0 不在数据面的进程树里**」：
   agent 不 fork、不 exec、不碰 cgroup（§14.2.7），worker 与槽位一路非 root。
   - 若把口径提高成"**全链路无 uid 0 进程**"，C3 **不达标**；可行路线（**C3 结构 + C2 原语**）
     已评估并记在 §11 第 1 条，**本次不做**。
   - 若口径其实是"**尽量缩小能力面**"，那 **C1 反而更优**（root 收敛到 3 个动词 + 路径白名单）——
     C2/C3 都用更宽的 `SETUID` 换掉了 `CHOWN`。**这三条并列写在这里，免得日后被误读成"C3 最安全"。**
   ⚠ 这一条决定方案成不成立，必须**先确认再写实施计划**。
8. **特权集中到 agent = 单点价值最高**：agent 能读写**每一个**租户的树。它一旦被攻破，
   后果比今天的节点 broker 更大 —— 范围是 fleet，不是单节点。必须配套：最小动词集、
   路径白名单、**全量调用日志**（谁在什么时候要求对哪个路径做了什么）、它**能被谁访问**
   （NetworkPolicy / RBAC）、以及"agent 不进数据面进程树"这条边界（A 形态天然满足）。
   A/B 形态的取舍也要按这条算：B 形态让 agent 进数据面进程树，攻击面比 A 大一档。

## 8. 收益的诚实量化

| 收益 | 强度 | 说明 |
|---|---|---|
| worker 特权面 3 动词 → 1 动词 | **强** | 那个动词已是最窄形态：程序钉死为 supervisor 绝对路径、uid 必须落在池内；再加 §4.3 可把 uid 决定权也拿走 |
| worker 容器里特权二进制 2 → 1 | 中 | 顺带删掉 `e2b-maint`（它今天在 k8s 里**本来就 exec 不了**：BND 只有 SETUID/SETGID，file caps 不是 BND 子集 ⇒ EPERM，留着是死重量 + 未来误加 CHOWN 的隐患） |
| 节点级常驻 root 组件消失 | 中 | 但只是搬到 CP |
| worker 可严格断言"零特权" | **强** | 对合规是**可二元验证**的命题，这正是 C1 换不来的那种性质 |
| 轴 B（隔离沙箱内部操作） | **零** | 不做 §6 就是零收益 |

## 9. 与 C1/C2 的关系

- **C3 是 C2 的替代，不是加强版。** C3 不需要 `e2b-as-uid`，也就不需要 C2 那两个必须裁定的
  设计点（§4.1 的 `rm --recursive` 属主校验、§4.2 的 setgid + `umask 007`）——
  因为**那两个点解决的都是"worker 以 X 身份创建/删除"的问题，而 C3 把这件事整个搬走了**。
  这是 C3 最被低估的好处：绕开了 C2 里唯一还没量清的部分。
- **相对 C2 的劣势**：CP 全面前置（热路径、延迟、协调复杂度），且轴 B 仍需另做。
- **回退性**：CP 侧的特权服务本身就是回退入口，所以 C3 的回退比 C2 更容易；代价是
  "常驻特权组件"从节点挪到了 CP —— 它没有消失。

## 10. 分期（每期能独立验收）

| 期 | 内容 | 验收 |
|---|---|---|
| P0 | 事实确认：① CP 的 root 与挂载在 k0s / ACK 两条 lane 上是否一致；② CP 主进程（`control_plane.combined_main`）今天**到底哪些动作真的需要 root** —— ✅ **已完成，见 §13**（结论：k8s 生产形态下主进程只剩一个动词）；③ `local://` lane 的定位；④ 合规口径（§7.7）；⑤ agent 形态 A/B 的最终裁定（§3.2） | 一页结论，不改代码 |
| P1 | CP 侧特权服务 + 路径纪律复用 `priv_common.c` | `tests/unit/test_priv_helpers.py` 同形断言 |
| P2 | 建树/拆树上移，开关（如 `E2B_TREE_MUTATOR=worker\|cp`） | 真机：新建箱 → `stat` 属主正确；`deployment_smoke` |
| P3 | 卷切片、检查点、secret、迁移导入 | 各带一条 NAS 用例；`multinode_smoke` |
| P4 | **孤儿回收与自愈重新设计**（§5.2）—— 最难，单独排期 | `multiworker_interference` + 崩溃点用例 |
| P5 | 收尾：worker 删掉 broker 通道与 `e2b-maint`；断言 worker 零特权 | 全套 lane + 真机 |
| P6（独立立项） | §6 的组位模型关闭（轴 B） | 单独一份评估 |

## 11. 写实施计划前必须先定的事

1. ~~合规口径~~ → **✅ 已定（2026-09-28，用户裁定）：口径 = 「数据面（worker + 槽位）无 root」，
   架构 = C3，agent 作为特权操作组件。** 三条含义要一起记住：
   - ✅ **worker 与它 fork 的槽位无 root，且 worker 零特权二进制**（§14.2.7 实测：进程树与 cgroup
     都留在 worker pod）；CP 收窄到零特权（§3.1）。
   - ⚠ **"全链路无 uid 0"这个更强的口径不在本次范围内**：agent 的**文件操作面仍然是 root**
     （NFS AUTH_SYS 上 `CAP_CHOWN` 不过网，§6 / `priv-broker.yaml` 文件头的 09-17 实测）。
     它之所以仍可接受，是因为**它不在数据面进程树里**：不 fork、不 exec、不碰 cgroup（§14.2.7）。
   - 📌 **"全链路无 uid 0"的可行路线已评估、记录在此，但本次不做**：需要在 C3 的结构外面
     **改用 C2 的原语**（agent 用"**以 X 创建 / 以 X 删除**"代替 chown）⇒ uid 0 可以归零。
     代价是 C2 的三条语义变更（§6 的 H2/H3 + 回收=删除）与 §5 那批位点在 NAS 上的重验；
     而且它把 `CAP_CHOWN` 换成**更宽**的 `CAP_SETUID` —— **"无 root" ≠ "无特权"**。
2. ~~CP 的 root 收不收窄~~ → **已定：收窄，特权归 agent**（§3.1）。
3. ~~agent 放每节点还是每集群~~ → **✅ 已定：每节点**。这不是偏好而是 (d) 的硬要求 ——
   写者要靠 `hostPID` 看见**同一节点上** worker 的 pid（§14.2.7 实测的 rendezvous 就是
   `NSpid:` 反查）。于是 §3.2 那张表的取舍被压成一边，**worker 拿到的是"0 特权"那一列**。
4. ~~`local://` lane 在不在覆盖范围内~~ → **✅ 已定（2026-09-28，按推荐）：不支持**。
   C3 只覆盖**分离形态**；`local://` 保留今天的形态，并在 lane 清单里点名（§11.1 第 4 项）。
5. ~~自愈走 a、b 还是 a+b~~ → **✅ 已定（2026-09-28）：(e) agent 巡检 → CP 决策 → agent 执行**。
   读者提出"自愈是不是可以走 agent"——**可以，而且比 worker 上报好**：把"眼睛"换成 agent 之后，
   盘面真相不再押在 worker 的诚实性上，也**不依赖 worker 活着**。**明确不选 (c)**（那是把删除
   拉回 worker，等于放弃 C3 的轴 A 收益）。详见 §11.1 第 5 项。
6. ~~§4.3 的策略文档上移做不做~~ → **✅ 已定（2026-09-28）：(c) 把"策略 + 身份"合并成一次授予**。
   `(d)` 之后身份已由 agent 授予（§11.2），剩下只有策略；让它跟 `uid_map` 同一次握手落地。
   **实现可以先接今天 worker 写的那份，但结构按"策略由 CP 给"设计** —— 将来换的只是接线。
7. ~~§6 是否同期做~~ → **✅ 已定（2026-09-28）：(c) 现在只做"收敛"，不做"关闭"**；并落四条
   一致性要求（X 的权威来源现在定死 CP / 树的访问路径收敛到一个接口 / 测试矩阵留出两档 /
   agent 侧不许依赖"worker 能读树"）。详见 §11.1 第 7 项。

8. ~~CP 的非 root uid 取哪个~~ → **✅ 已定（2026-09-28，按推荐）：先取 65534，专属 uid 后议**。
  零迁移成本先落地。⚠ **A5 的定位已更正（2026-09-28）**：它在 local lane 的
  `_destroy_local` 里，**生产不可达** —— 按"正确性顺手修"对待，**不是生产在漏**（§13.7）。

9. ~~worker→CP 的身份怎么绑~~ → **✅ 已定（2026-09-28）：凭据定身份 + 关键信息做一致性校验**，
   按 §11.1 第 9 项的三步校验规范落地（近期用 per-node key，目标态 mTLS）。
   ⚠ 结论没变的部分：**光靠"CP 从自己的记录推导"兜不住** —— 记录只能用请求**自称的** node 去查；
   必须先把身份钉在凭据上。**补充（读者提出）：观察到的源 IP 是有效的第二因子** ——
   凭据挡"没有 key"，源 IP 挡"偷了 key"。
10. ~~槽位由谁起 + worker 怎么做到零特权~~ → **✅ 已定：走 §14.2.7 的 uid_map 代写（d）**，
    不走 §4.3 的"零裁量"过渡档 —— **(d) 的改动面比 §4.3 还小**（worker 侧只加一次 `unshare` +
    一次请求；agent 侧只加一次 `write()`），却一步到位。
    ⚠ **三条约束必须一起落**：① **worker 必须天生 65534（dumpable）**，否则静默失效；
    ② agent 要有"**65534 + `SETUID`/`SETGID`**"的那个面（走 owner 规则，**别用 `SYS_ADMIN`**）；
    ③ **"写哪个 uid"由 CP 从记录推导**。
    - **rendezvous 也已实测**：容器 pid → 宿主 pid 用 `NSpid:` 反查即可（§14.2.7），
      不需要 comm 匹配这种探针手法。
    - 备选：fd 反向交付（§14.2.3，要重做 `W1SlotPool`）、`path` 传输（§14.2.6，最后手段）。

### 11.1 六项待定：决策书（2026-09-28）

每项给**问题 / 为什么必须定 / 可选项 / 推荐 / 判据**。标注「可推迟」的项不阻塞开工，
它们是在计划里分支的。

#### 4. `local://` lane 在不在 C3 覆盖范围内

**问题**：`local://` 是"控制面与 worker **同进程**"的形态（`control_plane/api/sandboxes.py::_provision_local`）。
§13.1 已量出 A2/A3 只在它这里走 CP。而 C3 的全部价值建立在"**CP 与执行者之间有边界**"之上
（决策 / 授权 / 审计三段）。同进程形态里没有这个边界。

**为什么必须定**：它决定测试矩阵、以及"什么算回归"。

| 选项 | 做法 | 代价 |
|---|---|---|
| **(a) 声明不支持** | C3 只覆盖**分离形态**（k8s + 分离 compose 栈）；`local://` 保留今天的形态 | 本地开发/smoke 与生产形态分叉；要在 lane 清单里点名。**Task 4 片 B 按 D23 把这条扩成"按名字排除"**：除 `local://` 外，autoscaler 的 docker pool（`deploy/compose/docker-compose.autoscale.yml` + `autoscaler/backends/local.py`）与单机示例（`deploy/compose/docker-compose.yml`）同样排除——两者在各自清单里显式写 `E2B_PRIV_HELPERS=off`（"没有文件操作能力"），因为它们也靠过 worker 镜像里的 file-capability 二进制，而 worker 镜像已不含它们 |
| (b) 也在 local 里"走一遍 C3" | 同进程内走同一条"以 X"路径 | **等于自己给自己发指令，审计价值为零**（同一进程既是决策者又是执行者） |
| (c) 让 local lane 消失 | 本地开发改起一个分离小栈 | compose 已经在这么做；但会动开发流程 |

**推荐 (a)**，并在 lane 清单里明确写出覆盖范围。
**判据**：清单里能一眼看出哪些形态在 C3 覆盖内、哪些"按今天形态保留"。

#### 5. 自愈（孤儿回收）归谁 —— ⚠ **本批里最难的一项**

**问题**：今天 worker **自己**在启动时扫盘、回收孤儿 uid、把 stale 树交回（`_startup_uid_reconcile`
/ `_reconcile_locked`）。C3 之后 worker 零特权、且树属于池 uid X ⇒ 它既不该也不能自己收。
但要回收就必须有人**看见**盘面、有人**有记录**、有人**能动手**——这三件事在今天同属一个进程。

**为什么必须定**：它是 C3 唯一的**可用性硬依赖**：不解决，worker 一崩盘上就不收敛，磁盘填满。

| 选项 | 做法 | 代价 / 风险 |
|---|---|---|
| **(b1) worker 报告 → CP 决策 → agent 执行** | 沿用已有的 `/internal/nodes/{node_id}/reconcile` 形状（body 已是 `{sandboxIDs, snapshotIDs}`） | 多一次往返；**worker 的"看见"成为承重项** —— 它被攻破就能让 CP 误判别节点的沙箱（⇒ 与第 9 项耦合） |
| (a1) CP 周期全量巡检 | CP 侧单飞扫描（Redis `try_claim` 是现成模板） | CP 需要 fleet 视图；**CP 不可用期间盘上不收敛** |
| (c) 给 worker 一个"只删自己名下"的窄能力 | 把删除拉回 worker | **与 C3 目标相抵**（worker 又有了特权面）；不建议 |
| **(e) agent 巡检 → CP 决策 → agent 执行** | **agent 是"眼睛"**（它能看全盘、本来就在节点上、被攻破也不算它说谎），**CP 是"脑"**（唯一有权威记录的人），执行还是 agent | agent 要多一个周期扫描与一次 CP 往返；**但它把 b1 对"worker 诚实性"的依赖整个去掉了** |

**推荐（2026-09-28 按读者提问改）：(e) 为主，a1 不必要，(b1) 只作为补充信号。**
读者问"自愈是不是可以走 agent" —— **可以，而且比 b1 好**：b1 把"盘面真相"押在 worker 的诚实性上
（它被攻破就能让 CP 误判别节点的沙箱），而 **(e) 把"眼睛"换成 agent**：
agent 看得全（不只自己名下）、被攻破也不构成"说谎"（它本来就是这个信任级别）、
**不依赖 worker 活着**。CP 仍然握着唯一权威的那半（记录），所以 §3.1 的"CP 决策"没有被破坏。
**明确不选 (c)**。
**判据**：① **worker 崩溃且不重启**时盘上仍在 N 分钟内收敛（这正是 (e) 相对 b1 的增益）；
② CP 滚动重启期间**不误删活沙箱**；③ 一个说谎的 worker 不能让 agent 动别的节点的树。
**⚠ 剩下的依赖性**：CP 的记录若**过期**，(e) 会照它删 —— 今天那层 `protected_elsewhere`
（"看不到 fleet 视图就整体推迟"）必须在 CP 侧重做。

> **✅ 2026-09-29 实施（Task 6）—— 形状、三档门、以及"谁是谁的身份"**
>
> **形状**：agent 周期扫描自己挂着的 `<workspaces>/*`（面 B，唯一挂了共享工作区的容器），
> 把**看见的 id** 报给控制面 `POST /internal/nodes/{agent_node_id}/agent/inventory`，body 只有
> `{"sandboxes": [id, …]}`（没有 path、没有 uid、没有"请删"）；CP 用**权威记录**判定"**没有任何
> 记录认领这个 id**"才叫孤儿，然后指令**同一个节点**的 agent 用既有的 `rm` 动词删（路径由 CP
> 从自己的记录/设置推导；agent 再独立做一次 `realpath` + 四根）。agent 不决定、不自己动手、
> 不持有授权表。**worker 自己的扫盘仍按 Task 4 的具名告警保持关闭**（`chown --worker` 正是
> §14.3 量到的那条越权），本任务把那个具名缺口补成上面的机制，而不是把它打开。
>
> **触发/周期**：首扫 `E2B_C3_AGENT_SCAN_INITIAL_DELAY_S=30`，之后 `E2B_C3_AGENT_SCAN_INTERVAL_S=120`
> ⇒ 判据① 的"**N 分钟**"= **2–3 分钟**（30s + 120s）。被 CP 具名推迟或报不出去的轮次按倍率退避、
> 封顶 `E2B_C3_AGENT_SCAN_BACKOFF_MAX_S=600`（既不成整舰队轮询，也不静默停摆）。
>
> **`protected_elsewhere` 在 CP 侧的重做 —— 三档门（各自有条用例，删掉任一条即红）**：
> ① **权威面必须是共享记录**（`_record_store` 存在）。进程内的记录集**不能**证明"舰队里没有记录
> 认领它"：CP 一重启那套集合就是空的，共享挂载上**每一棵活树**都会看起来无主 —— 这个形状下
> 巡检**惰性**（具名推迟），不是危险；
> ② **读到的每一条都要有答案**（`unreadable == 0`）：`_iter_stored_records` 一向**静默跳过**读不
> 出来的记录（它不能把 TTL 清扫一起拖死），而继承这个习惯的清扫会删掉"唯一一条读不出的记录"
> 名下的树；新增 `SandboxRegistry.fleet_id_snapshot()` 把跳过的条数**数出来**（墓碑不算
> unreadable："已删"是个答案）；
> ③ **枚举条数 == 舰队记录数**（`GET /internal/fleet/metrics` 的 `activeSandboxes`，Task 4 的评审
> 钉过的那条纪律）：两次**读**之间的竞态（新建/删除落地）会让两个数不一致 ⇒ 推迟。
>
> **身份怎么绑**（本项在文档里原本只有 worker→CP 那一半）：agent 报的是**自己**（D12：身份是
> **宿主**），所以身份路径也是 agent 的 —— 凭据是 `E2B_C3_AGENT_TOKEN`（**只**在 agent 自己的
> 面上被接受，`control_plane/auth.py::verify_agent_key`；它**不进** `all_internal_api_keys`，
> 否则一把 agent token 就等于舰队内部凭据）；地址由**主机键**查出来（k8s：agent pod 的 label +
> `fieldSelector spec.nodeName=<宿主>`，**不读 worker pod** —— worker 崩了且不重启时 worker pod
> 可能已经不在，而那正是这条巡检存在的理由；compose：`E2B_C3_AGENT_URL` 的宿主名）；再做**源 IP**
> 第二因子（拿不到期望地址就 403 具名拒绝，绝不放行）。
>
> **网络**：agent 从"今天一个连接都不主动发起"变成**发起一个**——`deploy/k8s/c3-agent.yaml` 的
> NetworkPolicy 从 `policyTypes: [Ingress]` 扩到 `[Ingress, Egress]`，出口**只**写
> `app: control-plane` 的 3000（判据在 `tests/unit/test_c3_agent_manifest.py`）。控制面侧本来
> 就没有 NetworkPolicy（它接收 worker/gateway 的 `/internal/**`），所以**不需要**为这条新增入口
> 规则；这条差别写在这里，免得下一位读者以为漏了一半。
>
> **具名失败**：报不出去（不可达/被拒/非 JSON）、CP 推迟（三档门）、agent 拒绝 `rm`（例如另一个
> agent 已经把同一棵树删了：共享挂载上每个 agent 都看得见全盘）、**worker 面不许要这条 op**
> （`file_ops.spec_for(..., caller=...)`：`remove-orphan-workspace` 只属 `self-heal`，worker 问就
> 具名 400）—— 全部有名有姓，且"removed"永远不会用来描述一次没发生的删除。

#### 6. 策略文档 / 身份参数的权威来源 —— **在 (d) 之后已经变形，需要重述**

**问题**：原 §4.3 的想法是"把 route-B 策略文档从 worker 上移到 CP 写"。**(d) 之后这条要重述**：
worker 不再 setuid，它是**自己** `exec sandlock-supervise --policy <P> --uid X` —— 也就是说
**X 和 P 仍然由 worker 提供**。

**但 (d) 顺带改了一件重要的事**：**身份 X 现在是 agent 授予的**（`uid_map` 由 agent 写）。
worker 可以**请求**任何 uid，但只有 agent 能**授予** ⇒ "变成哪个 uid"已经不在 worker 手里了。
**剩下的只有 P（策略文档）。**

| 选项 | 做法 | 收益 / 代价 |
|---|---|---|
| (a) 维持现状 | worker 写 P、worker 传路径 | worker 仍能**放宽沙箱的隔离策略**（比"改文件"更重：那是改隔离本身） |
| (b) CP/agent 写 P 到 worker 不可写处 | worker 只传"哪一份" | 收口彻底；要新增一次下发 |
| **(c) 把 P 和 uid 合并成一次授予** | agent 在写 `uid_map` 的同一次握手里**也把 P 落下去**（或返回一个受保护的 P 句柄） | **成本最低**，且它把"隔离参数由谁定"一次收口；(d) 的握手本来就要扩展 |

**推荐 (c)**（把 (d) 的握手从"授予身份"扩成"授予身份 + 策略"）。
**判据**：worker 无法让 supervisor 跑在一份**它自己挑的**宽松策略上。
**可推迟**：它不阻塞 P1/P2（那些位点先按今天的方式工作）。
**⚠ 但推迟不等于不管 —— 一致性要求**：现在就**不要让 worker 成为策略的权威来源**
（§11.2 那条分界）。具体做法：**agent 那侧按"策略由 CP 给"来设计，实现可以先接今天 worker 写的
那一份**，将来换的只是接线，不是结构。

#### 7. §6 轴 B（沙箱树对 worker 的可读性）是否同期做

**问题**：C3 完全不动轴 B —— worker 仍靠 `0770` 的**组位**读所有沙箱树里的每一个字节。
要"隔离沙箱内部操作"必须关掉组位模型（`0770` → `0700`，所有访问"以 X"）。

| 选项 | 做法 | 代价 |
|---|---|---|
| (a) 不做 | C3 只解决特权面 | 目标的后半句不达成（但前半句达成） |
| (b) 同期做 | worker 对树零直连 | worker 侧所有 walk/stat 变跨进程；动一批断言；**CP/agent 成为唯一能读全部租户数据的地方**（威胁模型变） |
| **(c) 现在只做"收敛"，不做"关闭"** | 把所有树访问收敛到一个接口/模块，**行为不变** | 现在几乎零风险；B 落地时只改实现、不动调用点 |

**推荐 (c)**。理由：轴 B 的代价（**数据集中**到 agent）与 C3 的代价（**特权集中**到 agent）
**性质不同**，捆在一起会让两件事都难验收。
**判据**：C3 落地后，"关掉组位"只需改一处实现 + 一批断言，不需要动调用点。
**可推迟**：不阻塞开工。

**⚠ 但"推迟"≠"不管" —— 现在就要为一致性钉四件事（2026-09-28 读者要求）**：

1. **不变量在两种模式下都成立**：无论轴 B 做不做，**"树出生即属于 X"** 不变。所以 **X 的权威
   来源现在就必须定死（CP）**，不能让 worker 成为 X 的来源 —— 否则将来打开轴 B 时，worker 已经
   靠着"能自己产生身份"活了很久，改不动了。
2. **worker 对树的访问路径现在就收敛到一个接口**（今天散在文件 API、记账 walk、`SandboxWriter`
   几处）。轴 B 落地时**只切这一个接口**，不动调用点。
3. **测试矩阵现在就留出"树可读 / 不可读"两档的位子**（第二档先 skip 也行）。否则将来加进来会
   漏掉一整类断言 —— 这个仓库对"没量的东西不算数"要求很高，别让轴 B 成为例外。
4. **agent 侧的实现不许依赖"worker 能读树"**：若 agent 的某个流程偷偷借了 worker 的组位视野
   （例如"让 worker 先把清单报上来"），轴 B 一打开就撞车。
   **这条与 §11.2 的分界、以及第 5 项的推荐 (e) 是同一件事的三面** —— 它们都指向
   "**眼睛和手都在 agent，worker 只有它自己那份**"。

#### 8. CP 的非 root uid 取哪个

**问题**（§13.6/§13.7 实测）：CP 要非 root，但今天盘上有四处挡路 —— `_volumes` 是 `0:0 755`；
`state/.uid_pool.lock` 是 `65534:65534 0600`；`E2B_IMAGE_CACHE_OWNER_UID=65534` 配上 resolver 的
"作品全给 cache owner"模型；`_runtime/<id>` 是 `0700 65534`。

| 选项 | 做法 | 代价 |
|---|---|---|
| **(a) CP = 65534** | 不迁移 | **零迁移成本**；但 CP 与 worker 在文件系统层面**不可区分**（同 uid）；且 §13.7 的 **A5**（CP 对 `_runtime` 的裸 rmtree）会从"静默失败"变成"硬失败" |
| (b) CP = 专属 uid | 一次性迁移 + 改 `lock` 与 image resolver 的共享模型 | 隔离更好；**一个真实的工作包**（§13.6 三条 + §13.7 的 0700 记录目录，而它**每条新记录都会再产生一个**） |
| **(c) 先 (a) 后 (b)** | 现在用 65534 上线 | 增量收益（"CP 与 worker 可区分"）在 C3 之后价值有限 |

**推荐 (c)**。**另有一条与选项无关、但定位要读准**：**A5** —— `_remove_local_tree_confirming` 里
沙箱树走 broker 的确认路径，配对的那个 `_runtime/<id>` 却是裸
`shutil.rmtree(..., ignore_errors=True)`（§13.7 实测：非属主下**静默失败且返回真**）。
**它是真缺陷，但生产不可达**（唯一调用者是 local lane 的 `_destroy_local`，而两个生产栈都是
`E2B_ENABLE_LOCAL_NODE: "false"`）⇒ 按**正确性顺手修**对待，**不要当成生产在漏**。

#### 9. worker→CP 的身份怎么绑 —— ⚠ **不写死，CP 就是特权放大器**

**问题**（§14.3 实测）：`_require_internal_key` 查的是**舰队共享**的 `X-Internal-Key`，
而身份来自**请求体**里的 `node_id`。C3 下 CP 要为 worker 的请求去指挥 agent ⇒ "谁在请求"必须可信。

**为什么"只靠 CP 自己的记录兜底"不够**：CP 只能用**请求自称的** node 去查记录。一个说谎的 worker
可以自称是 node B，然后请求对 B 的沙箱动手 —— CP 查记录会**通过**。
⇒ **身份必须先钉在凭据上**，记录只能做"这个对象归不归你"的第二道。

**✅ 定案（2026-09-28，读者提出"用凭据和关键信息做校验"）：三步校验，缺一不可。**

> **凭据挡"没有 key"，关键信息挡"偷了 key"。** 两层不是二选一 —— 它们的攻击成本不同。

**第 1 步 · 凭据 → 身份（唯一可信来源）**
`X-Internal-Key`（或 mTLS 的 SAN/CN）在 CP 侧映射到一个确定的 `node_id`。**这是身份的唯一来源。**

**第 2 步 · 请求自称 == 凭据推出**
URL/body 里的 `node_id` 必须**等于**第 1 步的结果，**不一致即拒**。
今天这条**完全缺失** —— `/internal/nodes/register` 直接采信 `body.get("address")`
（`control_plane/api/internal.py:40`），URL 里的 `{node_id}` 也从不与调用方比对。

**第 3 步 · 对象用 CP 自己的记录校验（"关键信息"）**

| 关键信息 | 今天 | 应改成 |
|---|---|---|
| `node_id`（URL / body） | **自陈** | 必须 == 凭据推出的 node |
| `address`（注册时 body） | **自陈** | 用**观察到的源 IP** 建档，或 CP 从 **k8s API** 查 worker pod 的 IP（CP 已挂 ServiceAccount） |
| 请求涉及的 `sandbox_id` | 未校验 | 必须在 CP 记录里**且被放在第 1 步推出的节点上**（"这个对象归不归你"） |
| 动作 | 未定义 | 动词白名单，且只允许对本节点对象的动作 |
| 路径 / uid | 由请求方给 | **全部由 CP 从记录推导**（§14.4 硬规则二） |

**第 4 层（纵深防御，非必需但便宜）· 源 IP 作为第二因子**
读者提出"CP 应该能记录 worker 的 IP"。**这条有效，而且我应该早点算上它**：worker pod 的包出不了
自己的 pod IP（CNI 路由 + 该 pod `CapEff=0`、BND 只有 `SETUID|SETGID`，**没有 `CAP_NET_RAW`**），
CP 经 Service ClusterIP 收到的**源 IP 就是 worker pod IP**（kube-proxy 保留源地址）——
所以"自称 node B"在源 IP 上就会露馅。**但它有三个前提，必须一起落**：
**不一致怎么办 → ✅ 拒绝（fail closed）**（2026-09-28 读者裁定）。但"拒绝"要成立，
必须先解决三件事，否则它不是打死攻击者，就是**把节点钉死**：

1. **期望 IP 必须来自可信源 —— 不能"从源 IP 学"。** ⚠ 这条最反直觉：如果 CP 拿"这次观察到的
   源 IP"去更新 node→IP 绑定，那么**拿着共享 key 的攻击者可以先把 node B 重钉到自己的 IP**，
   这一层立刻白做。**正解是 CP 去问 k8s API**（`node_id` 就是 StatefulSet 的 pod 名，
   CP 已挂 ServiceAccount），让绑定**不由 worker 参与**。
2. **必须在 worker 重启时自动跟住。** pod 名不变、**IP 会变**；硬钉一个 IP ⇒ 重启后该 worker
   完全说不上话 ⇒ **节点被钉死**。用 k8s 的 list/watch 让期望值跟着走，比"首次观察到就钉住"安全。
3. **必须看得见。** 拒绝要在日志/指标上**点名**（"node B 的请求来自 X，期望 Y"）。
   否则一次绑定漂移的表现是"这个节点莫名其妙全拒"，排查成本极高。

**失败模式要一起定**：CP 拿不到期望 IP 时（k8s API 不可达）**也拒绝**，不许静默降级 ——
但要告警，因为那是**全舰队级自伤**。选 fail-open 等于这一层在最需要它的时候消失。

**⚠ 两个必须实测/写进测试的前提**：
- **CP 看到的源 IP 真的是 worker pod IP 吗？** 若中间有代理/ingress/sidecar，所有 worker 会呈现
  **同一个 IP** ⇒ 这层变成**恒真的死代码**（我们以为它在守）。判据：**两个节点上的 worker 发来的
  请求，CP 看到的源 IP 必须不同。**
- **不许漂移**的钉子：任何人"给 worker pod 加 `CAP_NET_RAW`"、"在 internal API 前放代理"、
  "注入 service mesh sidecar" 都会**静默**让它失效。仓库已有
  `tests/unit/test_worker_manifest_permissions.py` 这类钉子，挂上去即可。

⚠ **并且它不是主防线**：源 IP 是**网络层位置**，凭据才是**密码学身份**。
**盲区是"同一节点上的其它东西"** —— 它同时有该节点的 IP 与可能的 key，两层都过。
所以**第 3 步（对象归不归你）仍然必需**。

| 选项 | 做法 | 代价 |
|---|---|---|
| (a) **per-node key** | 每 worker 一把，CP 存 key→node 映射 | 改动小；轮换要按节点做 |
| (b) **mTLS 客户端证书** | CP 用 SAN/CN 推导 node_id | 最强；但要引入 CA 与签发流程（今天 internal API 是 HTTP） |
| (c) 复用 `E2B_INTERNAL_API_KEYS` 按节点划分 | 底座已有（复数 keys） | 需要补"key→node"的映射语义与轮换纪律 |
| ~~(d) 不改~~ | 靠 §14.4 的"从记录推导路径/uid" | **不够**（见上：自报 node 即可绕） |

**推荐 (a) 起步、(b) 为目标**。(a) 能把这一格从"致命"降到"可控"；(b) 是终态。
**外加第 4 层（源 IP）**作为纵深防御 —— 它便宜，而且挡的正是"**偷到 key**"这一档
（见下面那条判据 ②）。
**判据（两支都要可测）**：
① 用 node A 的凭据去请求对 node B 的沙箱做特权动作 ⇒ **必须被拒**（凭据层）；
② **偷到 node B 的凭据**、但从 node A 的网络位置发出同样的请求 ⇒ **必须被拒**（源 IP 层）。
② 是那一层存在的理由 —— **只测 ① 的话，源 IP 那层是死代码。**

### 11.2 agent 的职责边界：**身份的唯一授予者**（2026-09-28 按读者提问收敛）

读者问："**其他需要特权操作沙箱的是不是也可以收敛到 agent 组件？**" —— **可以，而且可以顺势把
agent 的定位从"执行特权操作"收紧成一句更有力的话**：

> **agent 是这套系统里唯一能"授予身份"的组件。** worker 可以**请求**一个身份，但**不能自己
> 获得身份**；所有"成为某个 uid"或"改变某个 inode 属主"的动作，身份都来自 agent。

这句话把 §14.2.7 的 (d) 也纳入进来了 —— **agent 写 `uid_map` 就是一次身份授予**。而且它给出一条
**必须写死的分界**，否则"收敛到 agent"会退化成换位置：

| | 身份给谁 | 为什么 |
|---|---|---|
| **槽位**（(d)） | **给沙箱自己的进程** | 那就是沙箱的身份，本来就是它该有的 |
| **文件操作**（建树 / 解包 / secret / 删树 / 卷切片 / 迁移导入） | **不给任何 worker 助手 —— agent 自己执行** | 若写成"授予 X 再让 worker 去干"，worker 拿到的是「**成为 X**」而不是「**做这件事**」，它接下来干什么就管不住了。**授权必须收窄到"做这件事"。** |

**⚠ 推论（写给实施者）**：C3 里 agent 承接的那些文件操作**必须 agent 自己执行**，
不能实现成"agent 发身份、worker 干活" —— 后者只是把 `e2b-slot-spawn` 换了个位置，
worker 侧的特权面并没有真的消失。**(d) 是唯一的例外，因为槽位就是沙箱本身。**

**这条分界还顺带回答了一个边界问题**：agent 的**文件操作面是 root**（NFS 上 `CAP_CHOWN` 不过网），
而它的**映射写者面是 65534 + `SETUID|SETGID`**；两面都**不**把身份交给 worker。

#### 11.2.1 已知缺口（2026-09-29 记录，Task 4 片 A 的三轮评审）

> 这一节只记账，不改变上面的边界。三条都要在最终评审里看到；前两条是**有意保留**的窄口子，
> 第三条是**待后续 task 收口**的缺口。

1. **拆箱"已缺席"的判据与执行不是同一条路径**（评审 N5）：agent 形状下 worker 用**自己的**
   `<workspace base>/<id>`、`<state base>/_runtime/<id>` 判断"已经没了"，而删除是 agent 在**CP 推导的**
   路径上做的。两侧 base 配不一致的部署可能出现"报成功而树还在"。非安全问题（真正执行/不执行的是 CP 那条路径），
   且这类漂移在别处已经可见（磁盘报告看不到这棵树、沙箱文件从所有 API 消失）；要收紧应在**注册时**做一次
   配置一致性检查，而不是每次拆箱多加一次往返。
2. **`remove-checkpoint` 没有"已缺席"分支**（评审 N6）：它删的是**整个 store**（每个沙箱只有一张镜像，
   见 `envd_service/runtime/checkpoint_store.py` 的模块注释），而调用方传的是 `<store>/latest`；镜像在
   调用方 `is_dir()` 与 op 之间消失时会**具名拒绝**而不是当成"没有可消费的东西"。窗口只有一个 syscall 宽。
3. **平台磁盘账在 agent 形状下会读成"未知"**（评审 I-3）：`<state>/_runtime/.checkpoints` 里的 store 是
   沙箱 uid 的 `0700`，agent 形状没有可问的 broker，所以这一项测量返回 **unknown**（不再是 0），
   checkpoint 准入与心跳都按"未知 ≠ 空"处理（准入直接拒、心跳**省略**该字段）。把它真正**接回 agent**
   （CP 用自己记录里的沙箱枚举后逐条 `walk`）是 slice B/Task 6 的后续项 —— 注意"记录已不在"的孤儿 store
   在 CP 那侧会 404，所以接回去还需要一条"无记录也允许 walk 这个目录"的 op，属于设计决定。
4. **`local://` 车道的一个同类门**（评审 m-4）：`control_plane/api/sandboxes.py::_provision_local` 仍以
   `priv_helpers.active_helpers() is not None` 作为 per-sandbox uid 的前提 —— 与 Task 4 修掉的那两个门
   同型。它只是 `local://`（合体节点）车道，而 C3 明确不覆盖该形态（§11.1 第 4 项），
   所以这里**逐字不动**，记在此处以免后续评审把它当成漏改重新发现。
5. **平台账里还剩两处"分量级"的静默 0**（第四轮评审 minor）：`measure_platform_disk_bytes` 现在把
   "整账测不到"报成 unknown，但 `directory_cost(runtime_dir)` 失败仍被 `except OSError: pass` 吞掉、
   非目录子项的 `entry_size` 失败仍 `continue` —— 都是给总和少算一块而不出声。量级小（是分量不是整账），
   性质与 I-3 相同，记为后续项（要么也点名、要么让这两个分量用同一套 unknown 传播）。
6. **worker 自己骨架目录的两处 `rmtree(ignore_errors=True)`**（第四轮评审 minor）：
   `_delete_sandbox_runtime` 的 `_runtime/<id>` 非 agent 分支、以及 `<pure_rootfs_dir>/<id>` 的骨架。
   与 A5 同形（静默半删），但目标都是 **worker 自己的目录**、不在特权面（CP 那条 A5 已修），
   且它们是既有行为；记为已知缺口，若要收口就与 §11.2.1 第 3 条（平台态接回 agent）一并做。
7. **仍有 async handler 内联做文件层重活**（第四轮评审 minor）：`agent_export_sandbox` 的
   `tar.add(workspace, recursive=True)`（整棵树 + gzip）与 import 的 `write_bytes` +
   `_extract_sandbox_archive`。它们**不触达 agent 层**、也不是 Task 4 引入的，但与 I-2/`/metrics`
   同类；slice B 或 Task 7 的清扫可以顺手把它们移到 `asyncio.to_thread`。
8. **k8s：worker pod 必须显式 pin 身份**（第五轮评审补记；**Task 4 片 B 已落地**）：
   `control_plane/worker_identity_source.py` 只在 worker pod 的 `securityContext` 里**读到**
   `runAsUser`/`runAsGroup` 时才认为可信。`deploy/k8s/worker.yaml` 的 worker 容器现在**显式**
   写 `runAsUser: 65534` + `runAsGroup: 65534`（片 A 记录的缺口就是它："只靠镜像 `USER` ⇒
   pod spec 里没有值 ⇒ CP 记不到身份 ⇒ 每个需要身份的 op 具名 503"）。钉子：
   `tests/unit/test_worker_manifest_permissions.py`（文本 + 渲染两处）与
   `tests/unit/test_c3_agent_manifest.py::test_the_worker_is_on_the_agent_grant_path_and_carries_no_agent_secret`。
9. **compose（含 `deploy/stack/docker-compose.prod.yml`，D17 在范围内）**（第五轮评审补记；
   **Task 4 片 C 收口：选项 2 已落地**）：给 face B（`c3-agent-maint`）加的 `pid: host`
   是 D21 选项 2（agent 从内核读 worker 进程的 uid/gid）的**部署前提**，三个 compose 栈都设了。
   片 B 之前这条链缺的是 agent 侧代码（agent 只把 CP 指令里的 `worker.uid/gid` 写进子进程
   环境，没有任何"从内核读"的路径）⇒ 该车道当时走**选项 1 的 fail-closed 一侧**
   （`NoWorkerIdentitySource`：节点记不到身份，凡需要身份的 op 具名 503），而**建箱路径上的
   第一个特权步骤就是属主交棒**（`envd_service/agent.py:2869-2870` →
   `uid_pool.apply_sandbox_ownership` → `agent_fileops.chown_workspace`），所以那三个 compose
   车道当时**连建箱都完不成**（`Sandbox.create()` 直接 503，不是"建好了但身份操作不可用"）。

   **片 C 接上的形状（2026-09-29）**：CP 侧新增 `KernelWorkerIdentitySource`（`hostname` 形状
   即 compose 走它，`configured=True`、`kernel_verified=True`）——节点把 worker 上报的
   `worker_uid/gid` 记为**待内核确认的声明**，并在每条"以 worker 身份执行"的指令里带上
   **锚点**；agent 侧 face B 在锚点存在时按锚点解出 worker 自己的进程、读内核的有效身份，
   **用它**做 `--worker`/`--gid`，声明与内核不一致即**具名拒**且不 exec 任何 `e2b-maint`
   （k8s 车道不动：它的指令不带锚点，值仍是 pod spec 校验过的那一个）。实现与用例：
   `deploy/c3_agent/lookup.py`（`ProcLookup.worker_uid_gid`）、
   `deploy/c3_agent/app.py`（`WorkerCredentials.container_id`）、
   `control_plane/{worker_identity_source,api/internal,c3_agent_client}.py`；
   `tests/unit/test_c3_worker_kernel_identity.py`、
   `tests/unit/test_c3_fileops_forwarding.py`（compose 声明+锚点）、
   `tests/contract/test_c3_worker_kernel_identity.py`（真容器/真内核）。

   ⚠ **更正（裁定 D25，2026-09-29 真机验收后）** —— 上面那条"读取放在 face B 的子进程里、
   子进程按 `E2B_C3_AGENT_RESOLVER_UID`/`_GID` 运行"**被取代**，原因是它有一个当时没被量到
   的前提：**face B 是 root、能力集恰为 `CHOWN/DAC_OVERRIDE/FOWNER`（判据 4），它没有
   `CAP_SETUID`/`CAP_SETGID`，因此根本生不出那个降 uid 的子进程**。真机实测（本树 agent
   镜像，2026-09-29）：`CapEff=0xb` 时 `subprocess.run(user=65534, group=65534)` 直接
   `PermissionError: [Errno 1] Operation not permitted`；加上 `SETUID`/`SETGID`
   （`CapEff=0xcb`）才成功。于是 compose 车道的**每一个带锚点的文件操作都拒**
   （建箱路径的第一步就是 `chown-workspace`）⇒ 三个 compose 栈一个箱都建不成。
   要"降 uid 读 `/proc`"就必须给 face B 加两条 cap，即**改判据 4**；D25 选了另一条。

   **D25 的形状：锚点换成 container id，读取只碰世界可读的文件。**
   两条实测事实决定了它（本机 OrbStack，2026-09-29）：

   * `/proc/<pid>/ns/pid` 的 `readlink` 走 `ptrace_may_access` —— 只有**同 uid**（或持
     `CAP_SYS_PTRACE`）的进程读得到别人的命名空间，所以 face B 读不了（给能力等于让它能
     ptrace 控制面，不能用）；
   * `/proc/<pid>/cgroup` 与 `/proc/<pid>/status` 是 **world-readable**：face B 自己就能读，
     而 worker 的 cgroup 里**逐字带着容器 id**（`0::/../e4a98a0c528215e…`），Docker 又把
     同一个 id 的前 12 位设成容器的 **hostname**。

   于是：worker 在 register/heartbeat 里上报 `containerID`（= 从内核读到的 hostname，
   `envd_service/worker_identity.py::worker_container_id`）；CP 只做形状校验
   （`^[0-9a-f]{12,64}$`）并把它作为**锚点**下发；face B 用
   `ProcLookup.worker_uid_gid` 取 cgroup 路径**包含该锚点**的候选，再读它们的
   `/proc/<pid>/status`。**没有新能力、没有降 uid、没有子进程**，判据 4 的 `0xb` 不动。

   ⚠ **锚点的部署约束（写在代码注释旁，也必须写进运维手册）**：container id 是**运行时给的**
   hostname，所以 **compose 栈不得给 C3 worker 设 `hostname:`**。设了它 → 上报值不是容器 id
   （形状拒）或形状合法但与任何 cgroup 都不匹配（无候选拒）→ 该节点每个需要 worker 身份的
   文件操作**具名拒**（"reported no container id …" / "holds no process this agent can see"），
   **绝不会**静默落到别人的 uid 上。钉子：`tests/unit/test_c3_agent_manifest.py`
   （三个 compose 栈的 worker service 都不得出现 `hostname`；原来那条"worker 的 `user:`
   必须等于 resolver 默认 uid"的钉子随旋钮一起删除）。

   ⚠ **判据的放宽（同一轮 D4 的裁定）**：谓词从"该锚点下**恰好一个**进程"改成

   * **至少一个** cgroup 命中（零个 ⇒ 具名拒）；
   * 所有候选的 `(uid, gid)` 必须**一致**（不一致 ⇒ 具名拒）；
   * **不**要求进程集合唯一。

   为什么"一致"就够：一个容器的进程共享一个身份，所以容器内的候选多寡**不是值的歧义**；
   两个 worker 解到同一个锚点也无害（它们同 uid）；绝不能发生的只有"采用一个内核没有为
   worker 进程报出的值"。旧谓词恰恰把**正常状态**（槽位的 `sandlock-supervise` 就活在
   worker 的 pid namespace / 容器里，运维一句 `docker exec`、一个没回收的 zombie 也一样）
   判成歧义 —— 真机上它把 compose 车道压成**每 worker 只能有一个活槽位**（4 个箱的标准
   `multinode_smoke` 直接失败）。**不要**把唯一性规则"修回来"：
   `tests/unit/test_c3_worker_kernel_identity.py::test_several_processes_in_one_container_are_not_an_ambiguity`
   与 `tests/contract/...::test_a_busy_worker_is_not_an_ambiguity` 就是为它立的钉子。

   ⚠ **仍未收口的残留**：锚点本身仍是 **worker 自报**的（`containerID` 在 register/heartbeat
   里上报，CP 只做形状校验）。它比 pid namespace 强的一点是**可被内核复核**：agent 要求它
   出现在候选进程的 cgroup 路径里，所以"报一个不存在的 id"是具名拒、不是错身份；弱的一点是
   它不证明"这个进程**就是**那个 worker"（同机、同 uid 的候选彼此可读），要收口仍需一条
   compose 侧的更强 token（k8s 那条用 pod UID 的 `pod<uid>` cgroup 证）。影响面窄（同机、
   同 uid、身份值相同或仅 gid 不同），记此以免与上面的取舍混淆。
10. **stack 的 agent uid 池是两个 worker 池的并集，但旋钮是各自独立的**（Task 4 片 B 记录）：
   `deploy/stack/docker-compose.prod.yml` 的 face B 用 `E2B_UID_POOL_START`（默认 10000）+
   `E2B_C3_AGENT_UID_POOL_SIZE`（默认 **2000**，覆盖 worker-1 的 `10000..10999` 与 worker-2 的
   `11000..11999`），而两个 worker 各自的池是 `E2B_UID_POOL_START(_WORKER2)` /
   `E2B_UID_POOL_SIZE(_WORKER2)`（默认各 1000）。默认值恰好是并集，但**把 worker-2 的池挪到
   10000..11999 之外、又不同步抬 agent 的池**，会让 worker-2 的每一步在 agent 侧变成具名拒绝
   （`priv_common.c` 按 `START..+SIZE` 校验 `--uid`）——**安全、可见，但需要运维知道**。
   更好的形状是让 agent 的池从一个显式"本机所有 worker 池的并集"变量派生（或做成注册期一致性
   检查），记在 Task 5/6 的候选清单里。

> **只在任务报告或清单注释里记过的残余（2026-09-29 收口评审补记）**：下面三条此前只散在
> `.superpowers/sdd/**`（gitignored）或清单注释里，落到分支的账上，免得下一次评审把它们当漏改
> 重新发现。都不影响本次收口。

11. **worker 侧的形态探测与 CP 侧各写了一份**（第三/四轮评审的 seam）：
    `envd_service/worker_identity.py::_identity_shape` 自己读 `E2B_NODE_ADDRESS_MODE` + 探
    ServiceAccount token 来判 `k8s` / `hostname`，与 `control_plane/node_address.py` 的同一套
    形态判定**并行存在**。两侧判成不同形态时的表现是**误导性的告警**（worker 以为该报 container-id
    锚点、CP 以为该按 pod spec 校验，反之亦然），不是错误身份 —— 但值得收口成一处共用判定，或至少
    钉一条"两侧读到同一个值"的用例。
12. **`control_plane/registry/manager.py` 还留着一处 A5 形态的静默半删**：
    `cleanup_workspace` 里 `shutil.rmtree(record.workspace_dir, ignore_errors=True)`
    （`manager.py:2121-2123`）。与 Task 4 在 `control_plane/api/sandboxes.py` 修掉的 A5 同型
    （失败被吞掉 ⇒ 看着成功其实是半删），只是这条走的是另一条路。要收口就让它与 A5 同口径报错/点名。
13. **compose 车道的两个结构性缺口**（Task 6 的记录，落到这里）：
    ① compose 栈**没有 k8s 的策略层**（没有 NetworkPolicy 等价物），所以"agent 的出口只到
    `control-plane:3000`"这条**只在 k8s 成立**；② `deploy/compose/docker-compose.multinode.yml`
    **没有 Redis** ⇒ 没有共享记录 ⇒ self-heal 的**门 (a) 每轮都具名推迟**，那个车道的孤儿巡检是
    惰性的（清单注释里写了"给它一份记录存储就开"的触发条件，并双向 pin 住它保持关闭）。两条都与
    Task 5/6 记的"compose CP 还不是 65534"同批。

## 12. 结论

- 读者目标的前半句「worker 不做任何特权操作」，**取决于 agent 放在哪一层**（§3.2）：
  选"每节点 agent"可以真的做到零特权（fd 走同机 `SCM_RIGHTS`，存活/回收协议重做）；
  选"每集群 agent"则 worker 保留一个动词，但 §4.3 能把这个动词的**决策权**拿走，
  只剩"请求启动一个已存在的定义"。**建议先做后者** —— 它拿到的是零特权里最值钱的那部分，
  代价只有"仍 exec 一个钉死的启动器"。
- **本文初稿有两处需要更正**：① 曾把"namespace 继承"列为槽位必须留在 worker 的理由 ——
  **错**，槽位自足建 ns（§2.1）；② 曾判定"worker 零特权不可达" —— **过于绝对**，
  正确的说法是"取决于 agent 形态"（§2.3、§3.2）。
- 后半句「把最危险的沙箱内部操作完全隔离」**是另一条轴**（§6），C3 单独做不到；
  但 **C3 是让它第一次可行的前提**——因为它把 worker 对树的组位依赖整个抽走了。
- **C3 相对 C2 最被低估的优势**：它不需要 C2 那两个悬而未决的设计点（"删除怎么以 X 做"、
  "gid 这一位谁产生"），因为那些问题在 C3 里根本不存在 —— 建树与删树都不在 worker 手里了。
- **C3 的代价集中在控制面**：热路径、延迟、多副本协调、以及"CP 变成最高价值目标"。
  它不是"更小的权限面"，而是**权限的搬家 + 集中**——和 C2 一样，用一句"更安全"概括是不准确的。
- **加上通信规则之后（§14）**：`worker → CP → agent` 这条链是自洽的，而且 **worker→CP 的通道
  今天就已经存在**（`/internal/**`，含 `/reconcile`）。
  槽位归属由一条不变量定（§14.2.2）：**谁 fork 槽位，谁的进程树里必须有一个能变成 uid X 的进程**
  —— 但**这不要求 worker 自己持有能力**。2026-09-28 实测走通了 **(d)：worker 仍然自己 fork，
  由一个"与它同 uid（65534）、只握 `SETUID`/`SETGID`"的 agent 代写 `/proc/<C>/uid_map`**
  （§14.2.7）。于是 **"进程树/cgroup 归 worker" 与 "worker 零特权" 可以同时成立**，
  且 **`W1SlotPool` 一行都不用改**。
  规则"worker 不能直连 agent"**不排除** worker 零特权 —— fd 传递不看发起方向（§14.2.3）；
  **不要**改用 `path` 传输，那是 transport 1 被造出来取代的形态（§14.2.6）。
  这一节里真正会让方案从"能跑"变成"能审"的是 **§14.3 的身份绑定**：今天的
  `X-Internal-Key` 是**舰队共享**凭据，身份来自请求体。不把身份绑到凭据上，
  **CP 就是特权放大器**。
- **做之前**：先确认 §7.7 的合规口径，再定 §11 的十件事。本文在那些问题定下来之前，
  是"设计备选"，不是实施计划。

## 13. P0 交付物：CP 主进程的特权动作清点（2026-09-28 静态核对）

**问题**：§3.1 要求"CP 零特权"，但 CP 今天以 uid 0 跑 —— 到底哪些动作**真的**需要它？

**方法**：全量扫 `control_plane/**` 的文件系统写操作，逐条判"需要 uid 0"还是"只需要一个稳定的
非 root uid"。

**结论先给**：**大多数不需要 root**。在 k8s 生产形态下，主进程真正需要特权的**只剩一个动词**。

### 13.1 A 类：真的需要特权

| # | 动作 | 位置 | 为什么需要 | 替代归属 |
|---|---|---|---|---|
| A1 | 删沙箱树 `<workspaces>/<id>` | `api/sandboxes.py::_remove_local_tree_confirming` → `priv_helpers.remove_tree(..., on_error="raise")` | 树属**池 uid**，且父目录 `1777` 粘滞位 ⇒ 非属主删不掉（C2 文档 §6 H4 的 `D2`/`D4` 实测） | **agent**（今天**已经在走 broker**，只是客户是节点 broker，见下） |
| A2 | 建箱把树 `chown` 给池 uid | `api/sandboxes.py::_provision_local` → `apply_sandbox_ownership` | NFS AUTH_SYS 只认凭据 uid，`CAP_CHOWN` 不过网（2026-09-17 实测） | **agent**（或按 C2 改成"以 X 建"） |
| A3 | 卷根 `mkdir` + `chmod 0o1777` | `registry/volumes.py:224,235` | `mkdir` 需要 `<export>/_volumes` 的写权限；`chmod` 只需是属主 —— ✅ **实测确认属 A 类**：`_volumes` 是 `0:0 755`（§13.6 ①） | **agent**，或把 `_volumes` 根迁到 CP 的 uid 后由 CP 自己做 |
| A4 | 迁移导入的 `rmtree` + `mkdir` | `api/sandboxes.py:2019,2055` | 同 A1 | **agent** |
| A5 | 删 `_runtime/<id>`（与沙箱树配对的那一半） | `api/sandboxes.py:1820`、`:1845` 的 **裸** `shutil.rmtree(..., ignore_errors=True)` | 该目录是 `0700 65534`（§13.7），非属主删不掉；**而且失败是静默的**。⚠ **但生产不可达**（唯一调用者是 local lane 的 `_destroy_local`）⇒ 归"正确性顺手修"，不是"生产在漏" | **agent**；顺手把它也改成走 broker 的确认路径 |

**三条重要的收窄**（第 3 条是 2026-09-28 实测补上的，它**推翻**了本条早先的结论）：

1. **A2/A3 只在 `local://`（合体节点）形态下走 CP**。k8s 生产走 `_provision_remote`，那两条语义上
   属于 worker（`A3` 的祖先 o+x 半段更是明确 no-op —— `_widen_ancestors_for_tenant_uids` 注释：
   *"A separated control plane has no envd service and no tenant uids, and the import failure is
   then the no-op"*）。
2. **A1/A2/A4 三条在 k8s 生产上根本不是 CP 的行为** —— 沙箱树对 CP 是**只读**的（§13.8 的挂载清单）。
   它们只属于 `local://` 合体节点。
3. ⇒ **k8s 生产里 CP 的 A 类只剩 A3（`_volumes`）** —— ⚠ **A5 不算**（2026-09-28 更正：它在 `_remove_local_tree_confirming` 里，而那个函数只在 `_destroy_local` 的 local 路径上，两个生产栈都是 `E2B_ENABLE_LOCAL_NODE: "false"` ⇒ **生产不可达**；见 §13.7 的定位更正）。
   ⚠ 本节早先写的是"只剩 A1 和 A4"，那是错的 —— 见 §13.8。

⭐ **A1 今天已经是"特权外包"了** —— 代码注释原文：*"The removal goes through the worker's own
helper (in-process first, then the `e2b-maint` broker) instead of `shutil.rmtree(ignore_errors=True)`,
whose silent partial failure was indistinguishable from success."*
所以 C3 对 CP 侧的改动不是"新引入特权外包"，而是**换一个 client**。

### 13.2 B 类：只需要一个稳定的非 root uid（今天的 root 是偶然）

| # | 动作 | 位置 | 说明 |
|---|---|---|---|
| B1 | 建平台自己的目录（`_snapshots`/`_templates`/`_builds`/`_secrets`/`state`） | `app.py:477`、各 registry 的 `self._base.mkdir` | 只是"建自己的目录"，非 root 完全够 |
| B2 | 快照 `copytree` / `rmtree` | `registry/snapshots.py:427,599,565` | 同上，是 CP 自己的树 |
| B3 | 模板 build 目录、OCI tar | `api/templates.py:87,157,204,244,473` | 同上 |
| B4 | secret 文件写入 | `registry/secrets.py` | 同上（**前提**：`_secrets` 的属主与 CP 一致；若要交给池 uid，则落回 A 类） |
| B5 | 读 buildkit socket | pod 级 `fsGroup: 1000` | rootless buildkit 以 uid 1000 跑；**靠 fsGroup 就够，不需要 root** |

⭐ **一条反转的现状**：上面这些目录**今天已经是 65534 属主，不是 root** —— C1 wave 2 的
`deploy/scripts/migrate-state-owner.sh` 把 `PLATFORM_TARGETS` 那 8 条（`state`、
`workspaces/_migrate`、`workspaces/_snapshots`、`_images`、`_secrets`、`_snapshots`、
`_templates`、`_builds`）从 root 改成了 65534，集群实测 `chowned=8`。

于是 B 类的结论变得很便宜：**CP 改成非 root 后这些目录照样能写**（它们是 65534 的，CP 只要也用
65534 就还是属主）。代价是 **CP 与 worker 同 uid**，两者在文件系统层面不再可区分。要避免这一点，
就得把这些目录再迁一次到 CP 专属 uid —— `migrate-state-owner.sh` 就是现成工具（加 `--target`
并改 `CHOWN_UID`），这也是它第二次派上用场。

### 13.3 C 类：不在主进程里，但在同一个 pod

| 组件 | 特权 | 能否照搬目标 |
|---|---|---|
| `image-cache-init` initContainer（`runAsUser: 0`） | `chown` `_images` 给 65534，并**校验**结果 | ✔ 可让 agent 代做。compose lane 里它本来就是**独立的服务**（`image-cache-init`，`user: "0:0"`）—— 形态已经是对的 |
| `buildkit` sidecar（rootless，uid 1000） | `seccompProfile: Unconfined`；且**不能**设 `allowPrivilegeEscalation: false`（会让 rootlesskit 的 `newuidmap` 死掉） | ⚠ **拉不低**。manifest 注释明确：*"buildkit is a builder, not a sandbox: it legitimately needs the whole syscall surface"* |

### 13.4 D 类：主容器整体就是 uid 0

- `Dockerfile.control-plane-gateway` **没有 `USER` 指令**（`FROM python:3.14-slim`）；
  `control-plane.yaml` 主容器**没有 `runAsUser`**；pod 级 `securityContext` 只有 `fsGroup: 1000`。
- ⚠ 但它**不是纯粹的疏忽** —— compose 的注释把它写成有意的：
  `# Z-F7 C1: one shared cache, two uids (root control plane, 65534 workers).`
  所以 C3 要改的是**一个有意的决定**，不是一个 bug；这决定了它需要一次裁定，而不是一次顺手修复。

### 13.5 对方案的三个直接后果

1. **「CP 零特权」比预想的便宜**：主进程真正需要特权的是一个动词（删他人的树），B 类全靠"改 uid"
   就能摘掉，**不需要 agent 参与**。⚠ 但 uid 取哪个是有约束的 —— 见 §13.6 的结论：
   取 **65534** 零成本（代价是 CP 与 worker 同 uid），取专属 uid 则要一个独立迁移包。
2. **agent 的能力因此可以裁得比 C1/C2 都窄** —— 如果 B 类归 CP 自己，agent 只需要
   "以属主身份删除/接管别人的树"。对照：C1 = `chown`/`rm`/`walk` 三动词，C2 = 四到五个动词。
3. **buildkit 是唯一需要单独裁定的硬骨头**，它决定"**整个 CP pod** 零特权"还是"**CP 主进程**零特权"。
   若口径取后者，这一条可以先记为例外，但要写进文档，不能沉默。

### 13.6 三条留疑项的实测结果（2026-09-28，k0s 集群）

**方法**：经跳板机隧道 + **显式** kubeconfig 连到本项目集群（脚本自检通过：2 节点 / arm64 /
`v1.36.4+k0s`），在 `control-plane` pod 里对共享 PVC 做 `stat`。
CP 主容器身份实测 `uid=0(root) gid=0(root) groups=0(root),1000`（那个 `1000` 来自 pod 级 `fsGroup`）
—— §13.4 的"主容器整体是 uid 0"由此从静态推断变成实测。

```
/var/lib/e2b-sandboxes                        0:0         1777
/var/lib/e2b-sandboxes/workspaces             0:65534     1777
/var/lib/e2b-sandboxes/_volumes               0:0         755     ← 唯一的例外
/var/lib/e2b-sandboxes/_images                65534:65534 755
/var/lib/e2b-sandboxes/_secrets               65534:65534 755
/var/lib/e2b-sandboxes/_snapshots             65534:65534 755
/var/lib/e2b-sandboxes/_templates             65534:65534 755
/var/lib/e2b-sandboxes/_builds                65534:65534 755
/var/lib/e2b-sandboxes/state                  65534:65534 1777
/var/lib/e2b-sandboxes/state/.uid_pool.lock   65534:65534 600
```

**① `_volumes` 根 = `0:0 755` ⇒ A3 确认归 A 类。** 非 root 的 CP 在 `755 root:root` 下建不了
`<volume_id>` 子目录（`registry/volumes.py:224` 的 `mkdir` 会 EACCES）。**Task 5 用 D24 收口
（见下节的 ④ 与裁定）**。

> 附带观测：这个集群的 `_volumes` 里**只有 `_meta`，一个卷都没建过** —— 说明这条路径在生产上
> 还没被走过，"首个挂载沙箱当属主"（`_ensure_shared_volume_root`）那套语义**也还没被生产验证过**。

**④（2026-09-29，Task 5 的复核 —— 它推翻了"二选一里 agent 那条更省"的直觉）**：`_volumes`
不是 CP 在那里的**唯一**写。`VolumeRegistry._write_record` 把卷记录写成
`<store>/_meta/<volume_id>.json`（`registry/volumes.py:245` 的 `_record_path`），而建 `_meta`
的是 `write_text_atomically` 的 `directory.mkdir(parents=True, exist_ok=True)` —— "谁第一次写谁
当属主"，今天就是 root CP。所以**两条路都必须先做一次属主交棒**：只把 `<volume_id>` 的 `mkdir`
交给 agent，记录那一半仍然 EACCES。§3.2 的"**不需要磁盘迁移**"因此是不完整的 —— 本节那次实测
只 stat 了 `_volumes` 根，没有 stat `_meta`。

**★ D24 裁定（2026-09-29，Task 5）：走 ① 的第一条修法 —— 把 `_volumes` 一次性、非递归地交给
CP 的 uid（65534），CP 保留自己的 `mkdir` + `chmod 1777`。**

理由（也是为什么这是对 §3.2 **字面**的有意偏离 —— §3.2 第 2 步写的是"把 CP 剩下的 A 类动作
交给 agent"）：

1. **交棒既然不可省，agent 路线的剩余增量就全是净成本**：给 root 的 `e2b-maint` 加一条
   **`mkdir` 动词**，再给共享存储上的那个 op 定一条"发给哪个节点的 agent"的规则（卷记录的
   `node_id` 是 `local`，而 CP 是按节点寻址 agent 的 —— 硬规则 3 不允许从请求体里拿地址）。
   新动词跑的正是 **root** 文件面，**扩的恰是 C3 要收的那张面**。D24 两条都不需要。
2. **判据照样成立**：交棒之后 CP 的 `mkdir`/`chmod` 是**属主操作**，不是特权操作 ——
   §3.2 第 2 步的判据（"CP 侧 A 类清零；卷根建得出来、`_runtime` 删得掉"）说的是**结果**。
   `_runtime/<id>`（`0700 65534`）同理：CP 就是它的属主，§13.7 的探针早已量到
   `A-owner: ok (same uid means no privilege needed)`。
3. **它是计划自己的备选**：① 的"把 `_volumes` 迁给 CP 的 uid 后由 CP 自己做"、§13.2 的
   "B 类只需要一个稳定的非 root uid"、§13.5 的"A3 与 A1/A4 一起交给 agent，**或一次性迁属主**"。

**⚠ 两条硬性质（写进实现与钉子）**：

- **非递归是这条不变量本身**：`_volumes` 下面挂着卷数据目录与每沙箱配额切片，属主是**池 uid**。
  `chown -R` 会在**每次 agent 滚动**（文档里的升级步骤就会滚它）把它们抢回 65534。
- **它的能力集与面 B 逐条相同**（review round 1 的 minor 1）：`runAsUser: 0` +
  `drop: [ALL]` + `add: [CHOWN, DAC_OVERRIDE, FOWNER]`。此前它是那 pod 里**唯一**带 runtime
  默认能力集（含 `NET_RAW`）的容器，而判据 4 是按 pod 读的。三个里 `DAC_OVERRIDE` 是量出来的、
  不是类比来的：`mkdir -p "<65534:0755 的目录>/_oci"`（幂等重跑或半初始化的缓存）在只有
  `CHOWN`+`FOWNER` 时是 `EACCES`，`set -e` 会把一个健康部署变成 Init:Error。
- **幂等且有名有姓**：交棒由 agent 的 `storage-init`（`deploy/k8s/c3-agent.yaml`，每个节点一次）
  做，**每个目标各自一行状态**：`already belongs to uid 65534`（共享挂载上第二个节点落在这里）
  或 `-> uid 65534 mode …`（本次交的）；`_meta` 不存在时是 `does not exist -- nothing to hand
  over`（控制面以属主身份惰性建它）。**每个目标都各自校验**，不合格即 FATAL + `exit 1`，并在
  `stderr` 点名**那一个**路径与一次性命令 —— 而不是等到第一次建卷才在 CP 里报 EACCES。
  ⚠ **`_meta` 曾经是例外**（review round 1 的 Important）：它没有门、成功行还无条件打印，
  于是被拒时"报成功"，失败推迟到 D24 复核发现的那**第二次写**（`_write_record` →
  `write_text_atomically` 的惰性 `mkdir`/`os.open`，在 `VolumeRootNotOwnedError` 的包装**之外**）。
  现在两条都过同一套校验，成功行在门**之后**。CP 侧的具名失败是
  `control_plane.registry.volumes.VolumeRootNotOwnedError`（措辞**形态中立**：它说的是"这个 uid、
  一次非递归交棒"，不是 k8s 的某个脚本 —— review round 1 的 minor 3）。
- **判据改写**（brief 的第三条）："`_volumes` 的 `mkdir` 不在 CP 代码路径里" ⇒
  **"CP 拥有 `_volumes`，所以它的 `mkdir`/`chmod` 不需要特权"**，钉在
  `tests/unit/test_c3_cp_rootless.py`（清单 + 动词白名单 + 具名失败三处）。

**② `.uid_pool.lock = 65534:65534 0600` ⇒ 换 uid 会直接打断 uid 池。**
`uid_pool.py::_open_reservation_lock` 是 `os.open(path, O_RDWR | O_CREAT, 0o600)`；文件已存在时
`O_CREAT` 不起作用，非属主非 root 直接 EACCES。所以 **CP 非 root 时必须是 65534**，
或者把这个锁文件重新 chown/chmod，并把"两个身份都要能 flock 它"写进契约。
⚠ 这是**启动期就会炸**的一格（CP 一分配 uid 就碰它），不是边角路径。
（对照：`state` 本身是 `1777`，在里面**新建**文件没问题 —— 问题只出在这个**已存在的 0600 文件**。）

**③ `E2B_IMAGE_CACHE_OWNER_UID=65534` 与 root CP 是一对。**
`image_resolver.py` 的 docstring 把今天写成「两个生产身份：worker 65534 + **root control plane**」，
并明说 *"root writes it through `CAP_DAC_OVERRIDE`"*。三种情形：

| CP 的 uid | 结果 |
|---|---|
| root（今天） | 建完 chown 给 65534，之后靠 DAC_OVERRIDE 再写 ✔ |
| **65534** | 它就是属主，直接写，**不需要 DAC_OVERRIDE**，行为不变 ✔ |
| 其它 uid | 建完 chown 给 65534 之后**自己也写不了了** ⇒ 镜像缓存直接坏 ✘ |

**三条合起来指向同一个结论**：**CP 非 root 的最省事选择是直接用 65534**（与 worker 同 uid）。
两条 B 类路径（平台目录、镜像缓存）会照常工作；A3 与 A1/A4 一起交给 agent，或一次性迁属主
（**Task 5 选了后者，D24**；A1/A4 是 `local://` 车道，不在 C3 覆盖内）。

代价是 **CP 与 worker 在文件系统层面不可区分**。要避免，就得做一次真正的迁移：给 CP 专属 uid
+ 迁 `_volumes`（可能还要 `_images`/`_secrets`/`_snapshots`/`_templates`/`_builds`）
+ 改 `.uid_pool.lock` 与 image resolver 的共享模型。**这不是顺手改，是一个独立的工作包。**

#### 13.6.1 部署窗口的复验程序（Task 5 写下，**未执行**）

上面的表是 **2026-09-28 的基线**（CP 还是 `uid=0`）。Task 5 把 CP 换成 65534 之后，同一张表要
**在部署窗口里按下面的步骤重新量一遍** —— 那才是"CP=65534 后仍可写"的证据。真机由 controller
与用户协调，本节只写程序（**不含任何写操作，除了第 4 步那次显式的、一次性的交棒**）。

0. **先认集群**（`docs/deploy-clusters.md` §2 的自检；不加 `KUBECONFIG` 会打到另一套 ACK）：

   ```bash
   deploy/scripts/open-cluster-tunnel.sh
   export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"
   ```

1. **清单形态生效**（判据 8 的真机臂）：

   ```bash
   kubectl -n sandlock get deploy control-plane \
     -o jsonpath='{.spec.template.spec.initContainers}{"\n"}{range .spec.template.spec.containers[*]}{.name}{" "}{.securityContext}{"\n"}{end}'
   ```

   期望：`initContainers` 是空的（`image-cache-init` 已经搬去 agent 的 pod）；`control-plane`
   那一行是 `{"runAsGroup":65534,"runAsUser":65534}`；`buildkit` 仍在且是
   `{"seccompProfile":{"type":"Unconfined"}}`（**保留项，点名**）。

2. **交棒发生了没有**（agent 侧，每个节点一次）：

   ```bash
   kubectl -n sandlock logs ds/e2b-c3-agent -c storage-init --tail=60
   ```

   期望：**四个目标各一行状态** —— `.../_images`、`.../_volumes`、`.../_volumes/_meta`
   各是 `already belongs to uid 65534`（已交棒）、`-> uid 65534 mode …`（本次交的）或
   `does not exist -- nothing to hand over`（`_meta` 允许这一种：控制面以属主身份惰性建它），
   最后一行是 `.../_volumes is owned by uid 65534 (the volume data directories below it are
   left alone)`。**`_volumes` 与 `_meta` 是两条独立的门**（review round 1 的 Important：`_meta`
   从前没有门、且成功行无条件打印）；**看到 `FATAL` 就停** —— 它点名的是哪一个目标，并带着那条
   一次性命令。

   判别式（按脚本实际打印的字面量 grep）：

   ```bash
   kubectl -n sandlock logs ds/e2b-c3-agent -c storage-init --tail=60 |
     grep -E '(owned by uid 65534|already belongs to uid 65534|-> uid 65534|does not exist -- nothing to hand over)'
   ```

   四个目标都命中即为交棒完成；出现 `FATAL:` 就是没完成（按它给的那条 `chown` 做一次）。

3. **属主表复量**（本节那张表的 Task 5 版；判据"CP 侧 A 类清零"）：

   ```bash
   for pod in $(kubectl -n sandlock get pod -l app=control-plane -o name); do
     echo "== $pod"
     kubectl -n sandlock exec "$pod" -c control-plane -- sh -c '
       for p in workspaces _volumes _volumes/_meta _images _secrets _snapshots _templates _builds state; do
         stat -c "%n %u:%g %a" "/var/lib/e2b-sandboxes/$p"
       done
       id'
   done
   ```

   期望：`id` 是 `uid=65534 gid=65534`；`_images`/`_secrets`/`_snapshots`/`_templates`/`_builds`/
   `state` 是 `65534:65534`；**`_volumes` 与 `_volumes/_meta` 必须是 `65534:65534`**（D24 的
   直接判据；它们是 `0:0` 就是交棒没做，回到第 2 步）。`_volumes/<vol_id>/` 若已存在，**必须
   仍是它的池 uid**（非递归的反面证据）。

4. **`.uid_pool.lock` 可开**（§13.6② 的回归，启动期就会碰的那一格）：

   ```bash
   kubectl -n sandlock exec deploy/control-plane -c control-plane -- \
     python3 -c "import os; os.close(os.open('/var/lib/e2b-sandboxes/state/.uid_pool.lock', os.O_RDWR)); print('uid_pool.lock: ok')"
   ```

   期望：`uid_pool.lock: ok`（不是 `PermissionError`）。

5. **两条真实写路径各走一次**（判据"卷根建得出来"）：

   ```bash
   # 卷：CP 以 65534 在 _volumes 下建 <vol_id>，并写 _volumes/_meta/<vol_id>.json
   curl -sS -X POST -H "X-API-Key: $E2B_API_KEY" -H 'Content-Type: application/json' \
     -d '{"name":"c3-task5-window"}' http://<入口>/volumes
   # 建箱：走 deployment_smoke（它同时覆盖 spread / 命令 / 文件 / 卷 / 配额）
   E2B_API_URL=http://<入口> E2B_SANDBOX_URL=http://<入口> E2B_API_KEY=$E2B_API_KEY \
     python deploy/scripts/deployment_smoke.py
   ```

   期望：卷返回 `volumeID`，盘上是 `65534:65534 1777` 的目录 + `_meta` 记录；`deployment_smoke`
   全绿。**任一格不符就停在那里**（下一次 CP 滚动之前把属主改回来）。

   ⚠ 顺带把 **`Template.build` 也走一次**：控制面读 buildkit 的 unix socket，靠的是 pod 级
   `fsGroup: 1000` 给的**组位**（socket 由 rootless buildkitd 以 uid 1000 建在那个 emptyDir
   里）—— 从前 root 是靠 `CAP_DAC_OVERRIDE` 读的，现在 CP 是 65534，走的就是那条组位。
   socket 读不到时 `Template.build` 会直接失败（不是静默），所以这一格只需要有一条成功记录。

6. 结果回填 `docs/deploy-clusters.md` §7（现状节 + 发版记录），与 §13.6 这张表逐项对照。

> ⚠ 第 3 步的期望**不是**"所有条目都 65534"：`_volumes/<vol_id>/` 属于池 uid，这正是
> 非递归那条硬性质的现场证据。把它也写进期望值，否则下一次交棒改成 `chown -R` 时这张表
> 看不出来。

### 13.7 共享记录面的实测（`state/**`）—— 同一类坑还有几个

**问题**：`.uid_pool.lock` 是不是孤例？CP 与 worker 共享的记录里还有多少"已存在的私有文件"？

**实测**（同一份 PVC、同一个 pod）：

```
state/                                   1777  65534:65534
state/.uid_pool.lock                     0600  65534:65534
state/.state-base-migration.journal      0600  65534:65534
state/.route-b/                          0755  65534:65534
state/_runtime/                          0711  65534:65534
state/_runtime/.checkpoints/             0711  65534:65534
state/_runtime/<id>/  （09-25 起，新）    0700  65534:65534
state/_runtime/<id>/  （09-19/20，旧）    0755  65534:65534
state/_runtime/<id>/command-logs.jsonl   0644  65534:65534
```

**逐条判**：

| 路径 | CP 拿它干什么 | 非 65534 的 CP |
|---|---|---|
| `state/`（1777） | 在里面新建 | ✔ |
| `.uid_pool.lock`（0600） | `O_RDWR` 打开并 flock（**启动期就碰**） | ✘ EACCES |
| `.state-base-migration.journal`（0600） | **CP 代码里没有引用**（只在迁移脚本及其测试里） | — 不影响 |
| `_runtime/`（0711） | **只按确切路径访问，从不枚举** —— `control_plane/` 里没有任何 `iterdir`/`scandir` 打在它上面，所以只需要 `x` | ✔ |
| `_runtime/<id>/`（**0700**，新记录） | 读 `command-logs.jsonl`（`api/sandboxes.py:710`）+ 拆箱时删整个目录（`:1820`、`:1845`） | ✘ EACCES |
| `_runtime/<id>/`（0755，旧记录） | 同上 | ✔ |
| `_runtime/.checkpoints/`（0711） | C2 文档 §8 风险 2 已记 | — |
| `.route-b/`（0755，逐 uid 子目录 0711） | **CP 代码里没有引用**（纯 worker 的） | — 不影响 |

**⚠ 新发现，比 `.uid_pool.lock` 更值得注意**：`_remove_local_tree_confirming` 里，
**沙箱树走 broker 的确认路径（`remove_tree(..., on_error="raise")`），但和它配对的那个
`_runtime/<id>` 目录用的是裸 `shutil.rmtree(..., ignore_errors=True)`**（`:1820`、`:1845` 两处）。
两件事：

1. 它**只在 CP 是 root 时成立** —— 该目录是 `0700 65534`，非属主删不掉；
2. 而它失败时是**静默的**（`ignore_errors=True`）—— 这正是 W7 评审当初为沙箱树修掉的那类
   "静默半删"，在**配对的那一半还留着**。

⇒ 于是 A 类多算一条 **A5**（§13.1）：CP 删 `_runtime/<id>` 是"删 65534 的东西"，与 A1 同类，
却绕过了 broker 的确认路径。

> ⚠ **但它的可达性在本轮末尾被更正了（2026-09-28）**：`_remove_local_tree_confirming` 的
> **唯一调用者是 `_destroy_local`（local lane）**，而两个生产栈都是
> `E2B_ENABLE_LOCAL_NODE: "false"` ⇒ **A5 在生产里不可达**。
> **所以它该按"正确性顺手修"排队，而不是"生产在漏、优先修"。**
> 本节上面那些实测仍然有效 —— 它们证的是**那段代码写错了**；错的是当时我由此推出的
> "生产在漏"这个结论。

**结论**：`state/**` 上没有出现 `.uid_pool.lock` 之外的**必需**私有文件
（`.state-base-migration.journal` 与 `.route-b/` CP 都不碰），但出现了**同类且更隐蔽**的一条：
**记录目录 `0700` + 静默 rmtree**。这把 §11 第 8 条的答案进一步压向 **65534** —— 取专属 uid
要收拾的不只是 `_volumes`，还有这个已存在的 `0700` 记录目录，而它**每条新记录都会再产生一个**。

**实测（2026-09-28，`deploy/scripts/acceptance/probe_c3_a5_silent_rmtree.py`，
在 control-plane pod 里对**同一份 NFS** 跑；它调用的是**真实函数**
`_remove_local_tree_confirming`，不复制逻辑）**：

```
N-fixture-shape: ok (dir=0o700 65534:65534, parent=0o711 65534, want 0o700 + 0o711 both owned by 65534)
A-root:    returned=True survived=False -> ok (invisible today)
A-as-uid:  returned=True survived=True  child_error=None -> SILENT FAILURE
A-strict:  raised="PermissionError: [Errno 13] Permission denied: PosixPath('.../sbx_c3a5strict')" survived=True
A-owner:   returned=True survived=False -> ok (same uid means no privilege needed)
C3-A5-VERDICT=reproduced
N-cleanup: root_gone=True leftovers=[]
```

读法：

- **`A-as-uid`（uid 65533 调真实函数）→ 返回 `True`、目录**还在**、一个异常都没抛** ⇒ **A5 成立**。
- `A-strict`（同一身份，但 `ignore_errors=False`）→ `PermissionError` 点名那个目录
  ⇒ **失败是真的，只是被掩掉了**。
- `A-owner`（65534 = 属主）→ 成功 ⇒ 阻塞项**纯粹是"不是属主"**，与父目录模式无关。
- `A-root` → 成功 ⇒ 这就是它今天不可见的原因。

> ⚠ **探针自己也会错，值得记一笔**：第一次跑时 `A-owner` 报 `UNEXPECTED` —— 我把 `_runtime`
> 父目录建成了 root 属主（`0755`），于是 `rmdir` 被**父目录**的模式挡下，有一个 cell 是
> 因为**错误的理由**"失败"的。改成生产实测的 `0711 owner:owner` 后复跑，才拿到上面这张表。
> 这条教训写进了脚本 docstring：**fixture 必须逐项对齐生产实测，否则你会得到一个看起来对的错答案。**

### 13.8 CP 的可写面 = 8 条显式子路径（挂载清单就是边界）

这一轮顺带量到一条**比代码扫描更硬**的证据：**CP 主容器的根挂载是只读的**。
`control-plane.yaml` 把共享 PVC 挂成

```yaml
- name: shared
  mountPath: /var/lib/e2b-sandboxes
  readOnly: true                       # ← 根：只读
- name: shared
  mountPath: /var/lib/e2b-sandboxes/_builds   # 然后逐条 RW 子挂载：
  subPath: _builds
# … _images / _secrets / _templates / _snapshots / _volumes
#    / workspaces/_migrate / state
```

⇒ **CP 能写的就是这 8 条**：`_builds`、`_images`、`_secrets`、`_templates`、`_snapshots`、
`_volumes`、`workspaces/_migrate`、`state`。
**`workspaces/` 不在其中** —— 沙箱树对 CP 是只读的。

这条把 §13.1 的结论钉死了：

- **A1 / A2 / A4 在 k8s 生产上不是 CP 的行为**：建树、chown 树、删树都发生在只读的那半边上。
  第一次跑探针时正是撞上这个 —— 在 `/var/lib/e2b-sandboxes/_probes` 建目录直接
  `OSError [Errno 30] Read-only file system`（这也是探针最后把 fixture 挪进 `state/` 的原因）。
- 于是 **k8s 生产里 CP 的 A 类只剩 A3（`_volumes`，RW 且 `0:0 755`）**。
- ⚠ **更正（2026-09-28 复核调用链）**：本节初稿曾写"A5 因此是 k8s 生产上真实可达的那一条"。
  **那句是错的** —— A5 在 `_remove_local_tree_confirming` 里，而它的唯一调用者是
  **`_destroy_local`（local lane）**；两个生产栈都设 `E2B_ENABLE_LOCAL_NODE: "false"`
  ⇒ **A5 在生产里不可达**。它仍然是**真缺陷**（静默失败 + 返回真，已实测复现），
  但**是死代码里的缺陷**：按"正确性顺手修"对待，**不要按"生产在漏"对待**。
  本节的实测（§13.7 那张表）仍然有效 —— 它证的是**那段代码写错了**，不是"生产在漏"。

## 14. 通信与授权模型（三角分工的落地形状）

### 14.1 三条链路

```
worker ──① 上报──▶ CP ──② 指令──▶ agent
（跑沙箱）        （决策/授权）      （执行特权动作）
      ✗ worker ──▶ agent：禁止（**worker 不得发起**）
      ✓ agent  ──▶ worker：允许（agent 发起；见 §14.2.3，fd 就是走这条回发的）
```

| 链路 | 今天 | 需要 | 状态 |
|---|---|---|---|
| ① worker → CP（**上报**） | `/internal/**` + `X-Internal-Key` | **per-node 身份** | 通道已有，鉴权不够（§14.3） |
| ② CP → agent（**指令**） | **不存在** | token / mTLS | 新建；先例 `deploy/quota_agent/` 的 `E2B_QUOTA_AGENT_TOKEN` |
| ③ CP → worker（建/拆箱） | `record.envd_access_token`（per-sandbox） | 保持 | ✔ 已有 |
| ④ **worker → agent**（worker 发起） | — | — | 按规则**禁止**。⚠ **反向 `agent → worker` 是允许的**，而且它不是可有可无的 —— fd 反向交付走的就是它（§14.2.3） |

⚠ 规则 ④ 的含义常被读错，而读错的代价是整条路线被误判为不可行 —— 见 §14.2.1。

### 14.2 槽位由谁起：`worker 起父进程` **不等于** `worker 保留特权二进制`

> ⚠ **本节 2026-09-28 重写过。** 初稿写的是「worker **必然**保留 `e2b-slot-spawn`」与
> 「**唯一的出口**是换 `path` 传输」——**两句都错**，错因见 §14.2.1、更好的答案见 §14.2.3。§2.3 当初
> 写对了（"fd 由 agent 经同机 unix socket `SCM_RIGHTS` 转交"），是本节与 §2.3 矛盾。

#### 14.2.1 初稿错在哪：把「不能直连」读成了「没有连通的 socket」

正确读法只有一句：**不能由 worker 发起连接**。而 **fd 传递不看发起方向** —— `SCM_RIGHTS`
只要求两端有一条**已建立**的 unix socket，谁 `connect()` 都行。

#### 14.2.2 不变量：谁 fork，谁的进程树里必须有一个能变成 X 的进程

> **谁 fork 槽位，谁的进程树里就必须有一个能变成池 uid X 的进程。**
>
> ⚠ **2026-09-28 实测更正（第四次也是最后一次改这一节）**：本文曾把它写成「谁 fork，谁就必须
> **自己**能变成 X」，并推出「`worker 起父进程` ⟺ `worker 保留一个特权二进制`、**两者互斥**」。
> **"互斥"那句是错的。** 把某个进程变成 X 有两条路：自己持有能力，**或**由一个**与它同 uid 的
> 第三方**代写 `uid_map`。后者已在真集群上实测走通（§14.2.7），于是"进程树/cgroup 归 worker"
> 与"worker 零特权"**可以同时成立** —— 见 §14.2.2.2 的 (d)。

**这条不是本文的主张，是仓库已经写下的契约。** `envd_service/uid_pool.py` 的模块 docstring：

> *"Independent per-sandbox uids require a privileged (root) supervisor: **a non-root supervisor
> cannot map an arbitrary host uid** (S1.2 fail-closed contract), so allocation / ownership changes
> only happen when the worker runs as root. Non-root workers keep the fixed-uid + Landlock model."*

**为什么是内核规则而不是设计选择** —— 同一个事实在 fork 侧也写着
（`third_party/sandlock/crates/sandlock-core/src/context.rs`，两种情形并列）：

```rust
// Privileged path: the parent writes the `0 -> run_as` maps
// (only it still has CAP_SETUID in the parent namespace), ...
```
```rust
Some(_) if self_map => {
    // `0 -> our own host uid`: inside the namespace we are uid 0;
    // outside, every file and socket we touch is still owned by
    // the sandbox's host uid
```

⇒ **无特权进程只能把自己映射成自己**（self-map）。要得到 host uid = X（≠ 65534）的进程，
链路上必须有一个持有 `CAP_SETUID`（或 euid 0）的进程 —— 这是内核判据，改不了。

**为什么"worker 自己映射"走不通**：worker 是 65534 且 `CapEff=0`，所以它自己 `setuid(X)` 是
EPERM，它 fork 出的子进程也一样是 65534（同样 EPERM）。而把 `CAP_SETUID` 加进 **worker 进程
本身**方向是反的 —— 那等于把这份能力交给整个 Python 进程（它跑的代码远多于"起一个槽位"），
比给它一个只做一件事的二进制**更糟**。

**但那不等于"必须由 worker 自己持有能力"** —— 剩下那条路是 (d)：**让另一个与它同 uid 的组件
去写那个子进程的 `uid_map`**。这一步只需要 `write()` 一次，不需要 fork、exec，也不进进程树。
下一小节把四条路摆平，第六小节给实测。

##### 14.2.2.1 侧门逐条判（2026-09-28 用实测改写过）

| 路 | 判定 |
|---|---|
| 跨进程 `setuid` | ✘ **不存在**这个系统调用 |
| ptrace 注入 worker 的子进程 | ✘ 判据在 **tracee 的凭据**上，注入进去的 `setuid(X)` 照样 EPERM（顺带：file-capability 进程 non-dumpable，attach 本身就会被拒） |
| `SCM_CREDENTIALS` | ✘ 它传的是**身份声明**（给对端鉴权用），**不改变任何人的 uid**。这两件事常被混为一谈 |
| 给容器 `--cap-add SETUID`（给 worker 进程） | ✘ 那是把能力给**整个 worker 进程**，方向反了 |
| ⭐ **同 uid 的第三方写 `uid_map`** | ✅ **成立，已实测** —— 见 §14.2.2.2 的 (d) 与 §14.2.7 |

⚠ **最后一行曾经被本文判成 ✘**，原话是「user namespace + `uid_map`：不绕开，只是把特权搬进
`newuidmap`（**setuid-root** 的发行版 helper）」。**那句话把形态搞错了**：目标机上的 `newuidmap`
是 **file-cap `cap_setuid=ep`（0755）**，也就是说它**以调用者的 uid 运行**（65534），只是*另外*
握着一个能力。真正起作用的是 §14.2.7 测出的那条规则 —— **写者的 euid 必须等于目标 user namespace
的 `owner`**（"搬进 helper"这个描述把它归错了类，**而且归错的方向刚好像是在说它"绕不开"**）。

**"同 uid"这条是实测归纳，不是内核文档转述**：把写者从 root 换成"与目标同 uid"之后，错误码从
`EPERM`（Operation not permitted）变成 `EACCES`（Permission denied）—— **换了堵墙**。EACCES 那堵墙
是 **dumpable**：**非 dumpable 进程的 `/proc/<pid>/*` 归 root 所有**，65534 写者连开都开不了。
而真实拓扑里 worker **天生**就是 65534（没有 setuid 转换）⇒ 它的子进程 dumpable ⇒ `/proc` 归
65534 ⇒ 同 uid 写者写得进。⚠ 同一个事实还是一条**易碎的部署前提**，见 §14.2.7 约束 1。

##### 14.2.2.2 四条路（(d) 是 2026-09-28 实测补上的）

| 路 | worker 还 fork 吗 | worker 特权面 | 代价 |
|---|---|---|---|
| **(a) agent 起父进程**（§14.2.3 的 fd 反向交付） | 不 fork | 0 | **进程树与 cgroup 归 agent**（§14.2.5） |
| **(b) 放弃 per-sandbox uid** | 仍然 fork | 0 | 丢掉 E3.2 的核心产品属性：`E2B_PER_SANDBOX_UID=off` 时 *"non-root worker **degrades to the worker identity**"*（`uid_pool.py:42`；root worker 则映射到常量 `LEGACY_SHARED_UID = 1000`）。沙箱与 worker 同 uid ⇒ 不需要 `setuid`，两个特权二进制都不再需要 |
| **(c) 沙箱变成独立 pod** | 不 fork | 0 | uid 由容器运行时 / kubelet 定；整个架构重做 |
| **⭐ (d) 同 uid 的第三方代写 `uid_map`** | **仍然 fork** | **0** | agent 要多一个"**以 65534 跑、只握 `SETUID`/`SETGID`**"的面（§14.2.7 实测） |

**(d) 的形状**（2026-09-28 真集群实测走通，细节见 §14.2.7）：

```
worker(65534, 一个特权二进制都没有)  fork C ──▶ C unshare(CLONE_NEWUSER)     # 无特权
agent(uid 65534 + cap_setuid,cap_setgid)  写 /proc/<C>/uid_map = "X X 1"    # 唯一有特权的一步
C  setresuid(X) → exec sandlock-supervise   # C 是自己 userns 的创建者 ⇒ 持有该 ns 的 CAP_SETUID
```

agent **不 fork、不 exec、不进进程树、不碰 cgroup** —— 它只做一次 `write()`。
**而且 supervisor 的身份自检（`geteuid() == X`）照旧通过**：`X X 1` 是恒等映射，ns 内看到的就是 X。

**⭐ (d) 是唯一同时满足「进程树/cgroup 归 worker」与「worker 零特权」的那条路。**
本文此前写「**只有 (b)**」——那句在 (d) 被测出来之后就不对了。

##### 14.2.2.3 三档目标现在**全部可达**（走 (d)）

⇒ 「**agent 先通知、worker 再起父进程**」按 (d) 落地后：

| 想达到 | 走 (d) 可达吗 |
|---|---|
| worker **进程** `CapEff=0` | ✔ 今天就是这样 |
| worker 不能**选择**做什么特权动作 | ✔ 外加 §4.3：uid 从 **CP 的权威记录**推导，不从 worker 的 argv 读 |
| worker **不能触发**任何特权动作 | ✔ worker 只做三件**无特权**事：`fork`、`unshare(CLONE_NEWUSER)`、请 agent 写 map |

**⚠ 但 §4.3／§14.4 的硬规则一条都没少**：写者能往 `uid_map` 里写**任意** uid 值，所以
「**写什么**」必须由 CP 从自己的记录推导 —— 这与"路径不能由 worker 给"是同一条规则的两面。

#### 14.2.3 但 worker 零特权是可达的：fd 可以**反向**交付

```
agent（每节点）──connect──▶ worker                              ← 允许（agent 发起）
worker ──sendmsg(SCM_RIGHTS: control_fd, events_fd)──▶ agent    ← 同一条已接受的连接上回发
agent：fork → setgroups([]) / setgid(X) / setuid(X) → exec sandlock-supervise --control-fd N --events-fd M
```

worker **从头到尾没有发起过任何连接**，但它仍然把 fd 交了出去。

⇒ **规则 ④ 不排除 worker 零特权。** 代价是 agent 变成槽位的父进程（§14.2.4），
收益是 worker 容器里**一个特权二进制都不留**。

#### 14.2.4 剩下的是真依赖：谁当父进程（§2.2 的逐条替代）

| 用途 | 位置 | 替代 |
|---|---|---|
| 存活判定 | `.process.poll()` ×4（`:743` `:816` `:1401` `:1408`） | **通道 EOF** —— 同一个文件里已有 `_closed` 与 *"on EOF by itself"*（`:83` `:1404`），而 HANDOFF 记着这条性质**已被依赖**：*"worker 崩溃 ⇒ 通道 EOF ⇒ 槽位按 `finish()` 异常收口自杀"* |
| 优雅关停 | `.process.wait(20)`（`:828`） | 主路径本来就是控制通道上的 **`kill_child` verb**（`SlotChannel.kill` 的注释写明） |
| 兜底强杀 | `.process.kill()` + `wait(10)`（`:835` `:837`） | 让 **agent** 代杀（它是父进程） |
| 诊断 stderr | `.stderr.read()`（`:298` `:880` `:1293`） | 再发一个 **stderr 的 socketpair fd** 给 agent，接到 supervisor 的 stderr 上 |
| pid | `.process.pid`（`:825` `:833` `:843` `:1411`） | supervisor 的 `stats` verb **已经在报** pid（`:757`） |

**8–10 个调用点的有界改造**，三条已有现成机制。不是"动不了"。

#### 14.2.5 代价：agent 的手伸进了数据面

worker 交出去的是**控制通道**。今天 worker 握着它驱动沙箱；这个设计下 **agent 也握一份** ——
agent 不仅能读写沙箱文件，还能**直接向沙箱注入命令**。

⇒ agent 成为系统里唯一同时具备「全租户数据」**与**「全租户进程控制」的组件。
不一定是反对理由（它本来就能改文件，改了也影响行为），但**必须进威胁模型**（§7.8）。

> ⚠ **2026-09-28 更正两处**：
> ① **本节的前提是"agent 起父进程"**（即 §14.2.2.2 的 (a)）。若走 (d)（uid_map 代写），
>    **agent 不进数据面进程树**，本节整段代价**不成立** —— 这是选 (d) 的又一条理由。
> ② 本节初稿还写过 agent "**能直接向沙箱注入命令**"，**那条不准确**：worker 交出去的是
>    `server` 端（`handoff, server = socket.socketpair()`，`control_fd = server.fileno()` 给槽位），
>    而 worker 自己是**客户端**。所以 agent 能**冒充 supervisor 对 worker 说话**（伪造
>    `stats`/`launched` 之类应答），**不能**反向注入命令。前者是"对 worker 撒谎"，后者才是
>    "对沙箱动手"——**半径差一个量级**，而前者更阴（worker 的控制面被伪造且无从察觉）。

#### 14.2.6 那 `path` 传输呢：它是被取代的形态，不是出口

两条都能让 agent 起父进程，差别在凭据：

| | **fd 反向交付**（agent→worker + `SCM_RIGHTS`） | **`path` 传输**（registered socket） |
|---|---|---|
| 凭据 | **一条内核描述符**（不可读、不可猜） | socket 路径 + `--peer-uid` 白名单 + **token 在 argv** |
| 新增连接 | agent → worker（入站） | 无 |
| 新增共享路径 | 1 个 hostPath socket | **1 个 hostPath 注册表根**（见下） |

**`path` 传输看起来更干净（没有新连接），但它正是 transport 1 被造出来取代的那个形态。**
`docs/HANDOFF.md` 有一整节：

> ## ⚡ route-B transport 1：token 从 argv 消失（2026-09-09，SL-10 闭口 / fork F17）
> …route-B 槽位的凭证现在是**一条继承来的 unix 描述符**，argv 里没有 `--token`，
> `/tmp` 里也没有注册 socket（`sun_path` 108 字节约束随之消失）。

促使它被造的是一次**实测**：`/proc/<pid>/cmdline` 是 **0444 且不走 ptrace 门**（只有 `environ`
0400 被挡），foreign uid 21501 直接读出了另一槽位的 `--token <64hex>`。仓库自己的结论是
「**是暴露面，但被 peer 白名单兜住；配置漂移即成真漏洞**」（登记为 fork 侧 SL-10）。
同一次的三 uid 实测：`uid 0` 报错、别的租户 `21501` 被**静默关连接**、**沙箱自己的 `21500`**
（token 就在它可见的 argv 里）**同样被拒** ⇒ **兜住是真的，暴露也是真的**，两者的差别只在
`--peer-uid` 会不会漂。
（凭据构成已核：`priv_helpers.py:1073` 传 `--peer-uid`，`sandlock-supervise/src/main.rs:285`
用 `RegisteredPathChannel::bind_with_token(name, peer_uid, token)`。）

`path` 传输还有一条初稿没看到的成本：`_registry_sock_path` 返回的是
**`/tmp/sandlock-ctl-<uid>-registry/…`** —— **pod 本地、硬编码，不走 `E2B_ROUTE_B_TMP_ROOT`**
（那个变量指的是 NAS 上的槽位文档；worker manifest 里也没有 `/tmp` 挂载）。agent 起的槽位会在
**agent pod 的 `/tmp`** 建 socket，**worker 根本看不见** ⇒ 它**也必须**新增一条 hostPath 共享路径：
**它并没有省掉共享路径，只是换了一条。**

⇒ **在"agent 必须起父进程"这个前提下，选 fd 反向交付**：保住「描述符即凭据」，而不是退回 argv 里
的 token。

> ⭐ **但 2026-09-28 之后这个前提不成立了**：§14.2.7 实测走通了 (d)（**同 uid 的第三方代写
> `uid_map`**），于是 **worker 可以继续自己 fork**、进程树与 cgroup 留在 worker，而 agent 仍然
> 零 fork。所以本节真正的选择是三层：
>
> | 方案 | worker fork | cgroup 归 worker | agent 进数据面 | 凭据 |
> |---|---|---|---|---|
> | **(d) uid_map 代写**（§14.2.7 实测） | ✔ | ✔ | ✘ **不进** | 一次 `write()` |
> | fd 反向交付（本节） | ✘ | ✘ | ✔ 进 | 内核描述符 |
> | `path` 传输 | ✘ | ✘ | ✔ 进 | 路径 + peer 白名单 + argv token |
>
> **优先 (d)**；(d) 若在真实启动器上暴露新问题，退回 fd 反向交付；`path` 传输是最后手段。

#### 14.2.7 实测：(d) 在真集群上走通了（2026-09-28）

**探针**：`deploy/scripts/acceptance/probe_c3_userns_map_handoff.py`（三个 role：`forker` / `agent` /
`matrix`）+ `deploy/k8s-k0s/c3map-probe-agent.yaml`（`hostPID: true` 的 root pod）。
forker 在 `e2b-worker-0`（天生 65534），agent 在一个独立 pod（hostPID、先 root 后降到 65534 并保留
`CAP_SETUID|CAP_SETGID`）。

```
worker  N-forker-identity: euid=65534 gid=65534
        C-comm: c3map-probe05
        C-unshare: ok pid_in_this_ns=393

agent   N-agent-shape: uid=65534 euid=65534 CapPrm=0xc0 CapEff=0xc0
        A-target: host pid=2105658 (found by comm + empty uid_map)
        A-write: uid_map <- '10000 10000 1': ok
        A-write: gid_map <- '10000 10000 1': ok
        C3-MAPHANDOFF-VERDICT=agent-can-map

worker  C-setresuid: ok euid=10000 uid=10000 (inside the namespace)
        C-marker: .../05.txt written
宿主侧  stat -c '%u:%g' → 10000:65534      ← 决定性证据：真的是宿主 uid 10000
```

**agent 的形态**：`uid=65534`、`CapEff=0xc0`（**只有** `CAP_SETUID|CAP_SETGID`）—— 没有 root、
没有 `SYS_ADMIN`、没有 `SYS_PTRACE`、没有 `privileged`。这就是 `newuidmap` 在目标机上的 file-cap
形态。**跨 pod、跨 pid namespace、且不是父进程。**

**失败臂与它教给人的东西（都实测过，别重走）**：

先做了一次**失败点分离** —— 把 `open(O_WRONLY)` 与 `write()` 分开报，因为那是内核里两道不同的墙：

| 写者 | 目标 euid | 目标 dumpable | `open` | `write` |
|---|---|---|---|---|
| root + 默认 cap 集 | 0 | 1 | ok | **ok** |
| root + 默认 cap 集 | 0 | **0**（`prctl` 关掉） | ok | **ok** ← ⭐ **排除 dumpable** |
| root + 默认 cap 集 | 65534 | 0 | ok | ✘ `EPERM` |
| root + **`SYS_PTRACE`** | 65534 | 0 | ok | ✘ `EPERM`（**错的能力**） |
| root，**跨 pid namespace**（hostPID） | 65534 | 0 | ok | ✘ `EPERM` |
| **同 uid（65534）+ `CAP_SETUID|SETGID`，跨 pod** | 65534 | 1（worker 天生 65534） | ok | ✅ **ok** |
| **root + `SYS_ADMIN`** | 65534 | 0 | ok | ✅ **ok** ← ⭐ **预言命中（见下）** |
| 同 uid（65534），目标是**非 dumpable** | 65534 | 0 | ✘ `EACCES` | — |

最后一行那堵 `EACCES` 是**另一道墙**：非 dumpable 进程的 `/proc/<pid>/*` **归 root 所有**，
同 uid 写者连 `open` 都过不去（错误码从 `EPERM` 变 `EACCES` 就是这条线索）。

**⭐ 机理（2026-09-28 查内核源码 + 一次可证伪的预言命中）**

节点的内核是 `6.12.0-211.34.1.el10_2.aarch64`（RHEL 6.12 树，已 backport 上游那道检查）。
`kernel/user_namespace.c::map_write()` 里，**在** `new_idmap_permitted()` **之前**有一道先决检查：

```c
	/*
	 * Adjusting namespace settings requires capabilities on the target.
	 */
	if (cap_valid(cap_setid) && !file_ns_capable(file, map_ns, CAP_SYS_ADMIN))
		goto out;                       /* ret = -EPERM */
```

⇒ **打开这个 map 文件的进程，必须在"目标 user namespace"里持有 `CAP_SYS_ADMIN`。**

那"同 uid 写者"凭什么有？凭**同一个文件**里 `cap_capable()` 的那条 **owner 规则**：

```c
		/*
		 * The owner of the user namespace in the parent of the
		 * user namespace has all caps.
		 */
		if ((ns->parent == cred->user_ns) && uid_eq(ns->owner, cred->euid))
			return 0;
```

⇒ **处在目标 ns 的父 ns、且 `euid == ns->owner` 的进程，在那个 ns 里自动拥有全部能力** ——
包括这里要的 `CAP_SYS_ADMIN`。而 `ns->owner` 就是**创建那个 ns 的进程当时的 euid**。

**每一格都闭合了**：

| 写者 | 目标 ns 的 owner | owner 规则 | 结果 |
|---|---|---|---|
| root（euid 0），无 `SYS_ADMIN` | 65534 | 不触发 ⇒ 需要真 `CAP_SYS_ADMIN`，没有 | ✘ `EPERM` |
| root **+ `SYS_ADMIN`** | 65534 | 不触发，但**真有**该能力 | ✅ ok |
| **euid 65534 + 仅 `SETUID\|SETGID`** | 65534 | **触发** ⇒ 白得 `CAP_SYS_ADMIN` | ✅ ok |
| root（euid 0），目标以 root unshare | 0 | **触发** | ✅ ok ← **生产 root lane 就是这一格** |
| 同 uid，但目标**非 dumpable** | 65534 | —（这是 `open` 那堵墙，不是本题） | ✘ `EACCES` |

**可证伪性**：这条机理给出一个**预言** —— "给 root 写者加上 `CAP_SYS_ADMIN` 就应当成功"。
**实测命中**（上表第 7 行）。同时它也解释了先前那两格失败：**加 `SYS_PTRACE` 是加错了能力**，
而不是"判据不存在"。

**外部佐证**：生产里那条一直能跑的路径（`context.rs` 的 "privileged path"，**父进程**给**子进程**
写 map）之所以成立，正因为子进程是在 **`setuid` 之前** unshare 的 ⇒ owner = 0 = 写者 euid ⇒
owner 规则触发。**同一机理，两个独立来源。**

同一组矩阵还推翻了三个我先后提出、又被数据打掉的假设：**① "写者必须是父进程"**（sibling 与
grandparent 在目标是 root 时都写成了）、**② "瓶颈是 pid namespace"**（同 pid ns 下同样 EPERM）、
**③ "卡在 `ptrace_may_access`／`dumpable`"**（root 目标非 dumpable 也照样写成）。

**先记同轮的另两项实测，再列三条落地约束**：

**同一轮的另外两项实测（2026-09-28）** —— 它们把本文最初那两个要求从"推理"变成"证据"：

| 项 | 结果 |
|---|---|
| **生产级 rendezvous**（容器 pid → 宿主 pid） | ✅ worker 报容器 pid `425`，agent 用 `NSpid:` 反查到宿主 `2147848`；子进程 `426` → `2147849`。**不需要 comm 匹配这种探针手法** |
| **cgroup 归属**（"进程树/cgroup 留在 worker"到底成不成立） | ✅ 两者的 `/proc/<pid>/cgroup` **逐字相同**（`0::/../../../burstable/pod6d3cdd7b-…/cfee67…`）⇒ **子进程确实落在 worker pod 的 cgroup 里** |

> 那一次探针的子进程最后报 `EACCES` 写 marker —— **不是设计失败，是探针自己的目录**：
> `--pids-file` 让**父进程**先以 65534 建了 `drwxr-xr-x` 的目录，而子进程此刻已是 uid 10000，
> 写不进去。**这个 `EACCES` 反过来正是身份已变的证据** —— 如果它还是 65534，就写得进去。
> （host 侧 `stat` 的正面证据在上一轮的 `10000:65534`。）

1. **worker 必须"天生"是 65534** —— 即镜像 `USER 65534:65534`、**不能**以 root 启动再降权。
   一旦经过 setuid 转换，进程变 **non-dumpable**，其 `/proc/*` 归 **root** 所有，同 uid 写者会拿到
   **`EACCES`**（不是 EPERM）。这条链路会**静默断掉**，必须写进契约并盯住。
2. **agent 要多一个"以 65534 跑、只握 `SETUID`/`SETGID`"的面** —— 这不是"顺手"，是上面那条
   机理的直接推论。而且它**有两条路可选，选错了就退化成宽能力**：
   - **走 owner 规则（本设计选的）**：写者 euid == 目标 ns 的 owner（= worker 的 uid **65534**）
     ⇒ 在那个 ns 里**白得** `CAP_SYS_ADMIN`。写者只需要 `SETUID`/`SETGID`。
   - 走真能力：给 agent **`CAP_SYS_ADMIN`** —— 也成立（实测过），但 `SYS_ADMIN` 是**宽得多**的
     能力，而且会随 agent 的文件操作面一起被握在手里。**不推荐**，只作为备选记录。

   以 root 且无 `SYS_ADMIN` 跑写者会静默变成 `write: EPERM`。C3 里 agent 还有文件操作那一面
   （chown/rm，需要 root-ish），所以它会变成两个面。**这不是坏事**：映射写者那个面小到可以单独审。
3. **"写哪个 uid"必须由 CP 从记录推导**（§4.3／§14.4）—— 写者能写任意值，这一条没有因为 (d) 而放松。

### 14.3 硬规则一：身份来自凭据，**不来自请求体**（以及这条规则为什么不是可选的）

今天的 `_require_internal_key` 查 `X-Internal-Key`，而 `verify_internal_key` 的注释写着它承载的是
**轮换窗口**（*"every listed key is valid"*）—— 它是一把**舰队共享的 bearer 凭据**：
证明"你是这类组件之一"，**不是"你是 worker-1"**。身份来自 URL/body 里的 `node_id`，
**由调用方自己填**。

⚠ **本文初稿在这里写错了，2026-09-28 实测更正。** 初稿写的是"今天这还算温和（拿到 key 的 worker
能替别的节点对账、把别人的记录置为孤儿）…… **在新模型下**会变成致命"。**那是错的**：
C1 今天就已经有一条更直接的路，而且**不需要 internal key** —— 见下面这一格。

#### 实测：worker 身份可以接管任意池 uid 的树（`probe_broker_authorization_surface.py`）

broker 的授权**只有两条**：`priv_peer_allowed()`（peer uid/gid == 65534）与
`priv_resolve_allowed_path()`（`realpath` 后落在**舰队级**的四个根之一）。
**没有 per-sandbox、也没有 per-node 的判断。**

探针在真集群上跑出的链（broker pod 建 fixture、worker pod 发起）：

```
N-identity: euid=65534 gid=65534
N-tree-before: 0o700 10000:10000                    ← 一棵"像沙箱树"的树：池 uid 拥有
N-helpers: True                                      ← 连上 broker（socket 传输握手成功）
T1-direct-read: 'EACCES (as expected: not our tree)' ← 直接读：拒绝（这就是隔离本身）
T2-broker-reclaim: ok                                ← 一句合法协议动词：chown --worker
N-tree-after: 0o700 65534:65534                      ← 树变成 worker 的了
T3-read-after-reclaim: 'C1-AUTHZ-MARKER\n'           ← 读到了内容
C1-AUTHZ-VERDICT=no-sandbox-authorization
```

⇒ **拿到 worker 身份（uid 65534；任意一个 worker pod 的 RCE 就够）就能对舰队里任意一棵池 uid
拥有的树 `chown --worker` 给自己，然后读或删。** 而四个根是**两台节点上同一份 NAS 的同一批路径**，
broker 又没有节点概念 —— 所以"别的节点的沙箱树"根本不是它区分得开的对象。
**C3 不制造这条洞，它继承这条洞。**

> 附带观测（收尾时同一次 listing，`workspaces/`）：**5 棵属主 0 的老树**（Sep-17/18/19），
> 其中**两棵是 `0777`**。C2 文档 §4.2 末尾记的那批"永远回收不掉"的树这次在盘上直接看到了；
> 而且比那份记录更差一档 —— 文档写的是 `755 0:65534`，实测有两棵是 `0777`。

#### 但这正是 C3 的价值：它是第一个能关掉这条洞的形状

今天 broker 收到的请求是 `{verb, path, uid}` —— **没有任何可以授权的对象**：路径在四个根里
随便选，broker 是个节点本地 C 守护进程，拿不到"沙箱 → 节点"的映射。**这条洞在 C1 的形状里
关不掉。**

新模型的请求是 `{sandbox_id, action}`（§14.4），而 **CP 手里正好有权威的 `sandbox → node` 记录** ——
于是 CP **第一次**能回答"这个节点有没有资格对沙箱 X 做这个动作"。

⇒ **§14.3 与 §14.4 不是"加固"，它们是把授权点第一次做出来。**
反过来：如果新模型实现时**不做**这两条（CP 只转发、路径由 worker 给），那就是
**今天的洞 + 一个新组件**，一点没改善 —— 而且会因为"加了 agent、加了认证"而让人
**误以为修好了**。

**规则**：`{node_id}` 必须**与凭据绑定**。CP **从凭据推导**节点身份；请求体里的 `node_id`
只用于**比对**（不一致即拒），不用于取信。可选实现：per-node key、mTLS 客户端证书（SAN）、
或至少把 `{node_id}` 做成 key 命名空间的一部分。

> **2026-09-28 定案与补充**：完整的**三步校验规范**（凭据→身份 / 自称==凭据推出 / 对象用 CP
> 记录校验）写在 §11.1 第 9 项，含一张"关键信息"表。**并且补一层**：观察到的**源 IP**
> 作为第二因子 —— **凭据挡"没有 key"，源 IP 挡"偷了 key"**；**不一致即拒（fail closed）**，
> 但有三个前提必须先解决（期望 IP 来自 k8s API 而非"从源 IP 学"、重启时自动跟随、拒绝要可见），
> 详见 §11.1 第 9 项。
> ⚠ 我在此之前漏了这一层，并说过"记录兜不住"就没别的办法 —— **那句话是错的**。

（`E2B_INTERNAL_API_KEYS` 复数已经存在，底座是有的 —— 实施前先确认它今天是不是按节点区分的。）

### 14.4 硬规则二：worker 只报「哪个沙箱、什么动作」，**绝不报路径**

这条比规则一更容易被忽略，但它是**agent 路径白名单的唯一防线**。

agent 的白名单（复用 `priv_common.c` 的 `realpath` + 四根）只有在**请求方不能指定路径**时
才有意义。如果路径来自 worker，白名单就被 worker 遥控了 —— 它退化成"在四根之内任意选择"。

**规则**：

- worker 上报的字段是 `{sandbox_id, action}`（外加它观察到的**本地事实**，例如
  "我这儿还有哪些沙箱"）；
- **路径由 CP 从自己的记录推导**（`record.workspace_dir`、`sandbox_runtime_dir(...)` 等）；
- agent 收到的请求里是 **CP 算出来的**路径，且 agent 仍**独立**做一次 realpath + 白名单校验 ——
  **两道，不互相替代**。

### 14.5 自愈：接受 CP 是决策单点，但把"上报"做成既有形状

今天 worker 启动时自己扫盘回收孤儿（`_startup_uid_reconcile` / `_startup_reconcile_once`）。
新模型下它只能上报。

- **通道已经现成**：`/internal/nodes/{node_id}/reconcile` 就是"worker 上报本地差异、CP 决策"的
  形态（body `{"sandboxIDs": [...], "snapshotIDs": [...]}`）。把它扩成"并请 CP 代做特权动作"
  即可，**不必新建协议**。
- **要接受的事实**：CP 不可用 ⇒ 不能拆箱、不能回收、磁盘不释放；而 worker 磁盘满之后正是
  最需要回收的时候。这条没有补偿，只能承认。
- ⚠ **连带风险**：今天 worker 的孤儿判定靠 `_uid_pool.lock` 的 flock +「属主 ∈ 池 且 无记录」。
  **CP 没有 worker 的组位视野**（§13.8：沙箱树对 CP 只读），所以这个谓词要在"看不见树"的
  条件下重算。今天 worker 里那层 `protected_elsewhere` 保护（`deploy/k8s/worker.yaml` 注释记着
  一次实测：重启的 worker 把四棵活树都看成无主，全靠它才没删）**必须在 CP 侧重做一遍**，
  否则会出现"CP 判无主 ⇒ 删活沙箱"。

> **✅ 2026-09-29 实施（Task 6）—— 上报是谁发起、谁决策、谁执行**
>
> 落地形状与 §11.1 第 5 项那段实施记录一致（主机键寻址 + agent 自己的凭据 + 源 IP 第二因子；
> id-only 的报告；CP 侧三档门）。这里只补一句"今天到底谁在扫"：**worker 的
> `_startup_uid_reconcile` / `_startup_reconcile_once` 在 agent 形状下仍然不跑**（Task 4 的具名
> 告警逐字保留、`envd_service/app.py` 里那行 warning 仍在），而非 agent 形状里那条既有清扫
> **一字未动** —— Task 4 的评审钉过的两条契约（看不到完整舰队视图就跳过 + 具名 + 退避；id 条数对
> `/internal/fleet/metrics`）在它自己的用例里继续成立，本任务只在**有权威记录的那一侧**把同样的
> 纪律重做了一遍。

### 14.6 这套模型**不**解决的问题

- **轴 B 不变**（§6）：worker 仍能用组位读所有沙箱树。要"隔离沙箱内部操作"仍得另做那一刀。
- **uid 选择的约束不变**（§13.6/§13.7）：CP 改非 root 仍要面对 `_volumes`、`.uid_pool.lock`、
  以及那个 0700 记录目录。
- **agent 仍是最高价值目标**（§7.8）：它还是唯一能读写全部租户数据的东西。

### 14.7 一条正面事实（写进审计口径用）

§13.8 实测：**CP 的根挂载是 `readOnly: true`，8 条 RW 子路径里没有 `workspaces/`**。
所以"CP 控制、agent 操作"在这个仓库里不是**给 CP 提权**，而是**给 CP 已经有的"发指令"能力
配一个受认证的执行者**。

⇒ 三段分工每一段都能单独认证、单独限流、单独审计，而且审计链是完整的一句话：
「**哪个节点，在什么时候，请求了对哪个沙箱的什么动作；CP 依据哪条记录批准了；agent 实际做了
哪一次系统调用**」。这是这个形状相对 C1/C2 最实在的好处，也是它值得写实施计划的原因。

### 14.8 C3 的回退面（**C3 Task 7 之后**）

Task 7 之前，C3 的回退故事有一条"两条路并存"的便利：新树由 worker 建成，**老代码（C1 的 broker）仍能
`chown` 接管**，所以盘上的树在 C1 与 C3 之间是**双向可读**的（计划文件
`docs/superpowers/plans/2026-09-28-c3-privilege-consolidation.md` 的「回退」段就是按这个写的）。
**Task 7 拆掉了那座桥**（broker DaemonSet + `E2B_PRIV_HELPER_SOCKET` + worker 的 `wait-for-broker`
闸门全部退役，`E2B_PRIV_HELPER_TRANSPORT=socket` 变成启动期具名拒绝），所以现在要说清楚**回退面剩什么**：

| 想退回到 | 怎么退 | 代价 |
|---|---|---|
| **Task 2 的"槽位身份不走 agent"**（`E2B_SLOT_IDENTITY=spawn`） | 改 env，**并且必须回到含 file-capability 二进制的 worker 镜像** | `spawn` 要的 `helpers.slot_spawner`（`envd_service/route_b.py`）就是 Task 4 从 worker 镜像移走的 `e2b-slot-spawn`。没有它 `privileged_starter` 为假、route B **直接不可用**（`envd_service/executors/sandlock.py`），**不是** C3 之前的行为 ⇒ 只改 env 是**半安装**，要退就得**清单 + 镜像同批**退（和 `socket` 那把杠杆一样） |
| **Task 4 的"文件操作不走 agent"**（`E2B_PRIV_HELPER_TRANSPORT=exec`） | 改 env，**并且必须回到含 file-capability 二进制的 worker 镜像** | 出厂镜像里已经没有 `/var/lib/e2b-priv/` ⇒ 只改 env 是**半安装**（启动自检具名拒绝，不是静默降级）。要退就得**清单 + 镜像同批**退 |
| **C1 的"节点 broker 做特权动作"**（`E2B_PRIV_HELPER_TRANSPORT=socket`） | **不再是原地可切的开关** | 代码路径已删（`TRANSPORTS` 不含 `socket`），DaemonSet 清单也删了。要退回这个形状只能**整批 revert 到 C1 那一版**（清单 + 镜像 + 那个 DaemonSet） |

⇒ **一句话**：C3 的两个 env 开关 —— `E2B_SLOT_IDENTITY`（`spawn`）与
`E2B_PRIV_HELPER_TRANSPORT`（`exec`）—— **都只在"含 file-capability 二进制的 worker 镜像"上才
有效**，而出厂镜像已经把那些二进制移走了，所以两者都不是"翻一个 env 就回到从前"的杠杆，都得
**清单 + 镜像同批**退。再往前的形状（root worker / C1 broker）更是"整批 revert 镜像 + 清单"。
盘上的数据不受影响：树仍是
`0770 owner=<池 uid> group=<worker gid>`，**任何 root 进程都能接管它**（这正是 §5.4(b) 那条 NFS
语义的另一面）——所以整批 revert 不会丢数据。运维口径与 `docs/k8s-deployment.md` §24.2 的回退节逐字一致。

> **相关**：N47（broker 的授权面）随本次退役关闭、残余挪到 N49；N48（属主 0 老树）按 2026-09-29 的
> 实测关闭 —— 两条的裁决与证据见 `docs/open-issues.md` 与 `.superpowers/sdd/task-7-report.md`。

## 15. 参考

- **同族路线**：`docs/c2-ownership-frontload.md`（C2，本文的替代对象）、
  `docs/deploy-clusters.md` §7.1–§7.4（C1 现状）与 §7.9（C3 Task 7 的退役）。
  `deploy/k8s/priv-broker.yaml`（文件头 + cap 集注释，记着 NAS/`CAP_CHOWN` 不过网与 peer 门实测）
  **已由 C3 Task 7 删除**：那些实测的现行落点是 `docs/production-deployment-requirements.md` §5.4(b)
  与 `docs/deploy-clusters.md` §7.3；要读原文就看 git 历史。
- **CP 侧先例与协调**：`deploy/quota_agent/`（"worker 外包特权给服务端 agent"的现成形态，
  注意它在 k8s 里没部署）、`docs/control-plane-multi-replica.md`（Redis + `flock` 协调）、
  `docs/production-deployment-requirements.md` §2.4.4（W4）。
- **路径纪律（要复用，不要重写）**：`deploy/priv/priv_common.c`、`deploy/priv/maint.c`
  （usage 头把 `chown`/`rm`/`walk`/`serve`/`ping` 五种形态写全了；直连形态就是 CP 侧作业要用的形态）。
- **槽位身份的源码依据（§2.1 的更正）**：
  `third_party/sandlock/crates/sandlock-core/src/context.rs`（`unshare(CLONE_NEWUSER)`、
  self-map `0 -> euid`、"only the launcher is responsible for dropping privileges" 的上下文）、
  `third_party/sandlock/crates/sandlock-supervise/src/main.rs`（模块头：**本二进制不携带任何
  capability**，`geteuid() == X` 由启动器负责）。
- **§14.2.2 不变量的依据（仓库已经写下的契约，不是本文的主张）**：
  `envd_service/uid_pool.py` 的**模块 docstring** —— *"Independent per-sandbox uids require a
  privileged (root) supervisor: **a non-root supervisor cannot map an arbitrary host uid**
  (**S1.2 fail-closed contract**)"*；同处还写着 `E2B_PER_SANDBOX_UID` 关闭时
  *"degrades to the worker identity"*，以及 `LEGACY_SHARED_UID = 1000`（§14.2.2.2 出口 (b) 的
  那个形态）。内核侧的并列注释（privileged path vs `self_map`）在
  `third_party/sandlock/crates/sandlock-core/src/context.rs`。
- **§14.2.7 的实测（(d) uid_map 代写）**：
  探针 `deploy/scripts/acceptance/probe_c3_userns_map_handoff.py`（三个 role：
  `forker` 在 `e2b-worker-0` 里跑；`agent --agent-keep-caps-uid 65534` 在下面那个 pod 里跑；
  `matrix` 做写者/目标身份的对照矩阵）+
  `deploy/k8s-k0s/c3map-probe-agent.yaml`（`hostPID: true` + `runAsUser: 0`，跑完 `kubectl delete -f`）。
  同族的历史调研：`docs/reports/task-usernsprobe-report.md`（它测到 `newuidmap` 可行，但把原因
  归给"helper + subuid"）。
  ⚠ **那份报告的归因是错的，而它躺在冻结归档里、不许改** —— `tests/unit/test_docs_only_point_at_repo_artifacts.py`
  的模块 docstring 明写：*"`docs/reports/**` … those files are **byte-exact copies of historical
  work notes**, so their `tmp/` mentions are the past narrated, not instructions"*。
  **所以更正落在这里，不落在那里**：真正起作用的是 §14.2.7 测出的「**写者的 euid == 目标 ns 的
  `owner`**」，与 "helper"、"subuid" 都无关（`newuidmap` 能工作，恰恰是因为它 **file-cap 形态、
  以调用者 uid 运行**）。**读到那份报告时，按本节为准。**
- **源码锚点（按函数名引用，行号会漂）**：
  `envd_service/priv_helpers.py::Helpers.slot_spawner`（§2 的 `pass_fds`）、
  `envd_service/app.py::_startup_reconcile_once` / `_startup_uid_reconcile`（§5.2 的自愈）、
  `envd_service/uid_pool.py::_reconcile_locked`、
  `control_plane/api/sandboxes.py::{_provision_local,_provision_remote}`、
  `control_plane/api/volumes.py`、`control_plane/app.py`（§4.2 的 CP 侧既有文件工作）。
- **部署清单**：`deploy/k8s/control-plane.yaml`（CP 的 root 与挂载）、`deploy/k8s/worker.yaml`
  （worker BND = `{SETUID, SETGID}`）、`deploy/k8s/priv-broker.yaml`（要退役的那个）。
- **P0 的两条实测（§13.6–§13.8）**：探针
  `deploy/scripts/acceptance/probe_c3_a5_silent_rmtree.py`（在挂了共享 PVC 的 root 容器里跑：
  `env PYTHONPATH=/app python3 probe_c3_a5_silent_rmtree.py --root <某个 RW 子挂载下的目录>`；
  ⚠ CP 的根挂载是只读的，fixture 只能落在那 8 条 RW 子路径之一，见 §13.8）。
  连接方式见 `docs/deploy-clusters.md` §0/§2（必须显式带 `KUBECONFIG`，先跑自检）。
- **§14.2.6 的证据：为什么不用 `path` 传输**（token 落 argv / `/proc/<pid>/cmdline` 0444）：
  - `docs/superpowers/plans/2026-09-09-envd-route-b-wiring.md` —— **最精确的结论所在**：
    裁定表**第 6 行**（"registered 形态的 channel token 只能走 argv……`/proc/<pid>/cmdline` 0444，
    **不受** ptrace 门约束" ⇒ envd 默认改用 transport 1），以及它上面那张**三 uid 实测表**：
    `uid 0` = 错误；别的租户 `21501` = **静默关连接**（拿不到任何 verb）；**沙箱自己的 `21500`**
    （token 就在它可见的 argv 里）**同样被拒**；结论是「**已闭口（SL-10）**」，同时保留一句
    「**这仍是必须记录的暴露面**：任何一次 `ps`/coredump/审计日志都会把 token 落到别人眼前，
    且 `--peer-uid` 一放宽就立刻变成真漏洞」。
  - `docs/HANDOFF.md` 的同名节（《route-B transport 1：token 从 argv 消失》，2026-09-09，
    SL-10 闭口 / fork F17）—— 含那条**被实测推翻的初版判断**（"跨 uid 读 cmdline 需要 ptrace 权限"
    → `cmdline` 0444 且不走该门），以及"worker 崩溃 ⇒ 通道 EOF ⇒ 槽位自收口"这条**已被依赖**的性质。
  - 实测探针 `deploy/scripts/acceptance/rb_token_probe.py`。
  - fork 侧：`third_party/sandlock/docs/supervise-identity-handoff.md`（SL-9/SL-10）、
    `third_party/sandlock/docs/test-baseline.md`（"argv keeps the secret out, SL-10"）。
