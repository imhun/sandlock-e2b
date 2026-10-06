# N83：每沙箱一个 cgroup 实施计划

**Goal:** 让"一个沙箱花了多少 CPU"变成**内核记账、内核强制**，从而把通知限流从"替 supervisor
记账"的位置上换下来 —— 洪泛花的是沙箱自己的额度，超了像任何 CPU 密集负载一样被节流，邻居无感。

**Spec / 依据:** `docs/open-issues.md` 的 N82 行（限流器按通知条数计费 + "关掉限流"的实测）与
N83 行；实测细节见 `docs/deploy-clusters.md` §7.46。

## 现场事实（2026-10-06 实测，决定了方案的形状）

| 事实 | 读数 |
|---|---|
| 沙箱的进程在哪棵 cgroup | **worker pod 的容器 cgroup**（`…/pod<uid>/<container-id>`）：`sandlock-superv`、`sandlock-init`、payload 三者同 uid（池 uid）、同 cgroup |
| worker 能不能建子 cgroup | **不能**：`/sys/fs/cgroup` 挂载是 `ro,nosuid,nodev,noexec`（`cgroup.controllers` 有 `cpu io memory pids`，`cpu.max=400000 100000`） |
| agent（面 B，root）能不能建 | **不能**：私有 cgroupns（`0::/`，看不到 `kubepods*`）；`/proc/1/root/sys/fs/cgroup` 被拒（**刻意没有 `CAP_SYS_PTRACE`**） |
| agent 能读什么 | 任意 `/proc/<pid>/cgroup` 与 `/proc/<pid>/stat`（0444，hostPID）⇒ **路径不必拼名字，从 pid 反查**；也能按 uid 汇总整棵树（Phase 0 的输入） |
| 沙箱自己的额度今天强制了吗 | **没有**：fork 只在 `max_cpu < 100` 时才起 `sandbox_throttle_cpu`，而 E2B 传 `min(100, max(1, cpu_percent))`、默认 100 ⇒ 不节流。实测 4 个自旋把 worker pod 拉到 **3832 mcore**（pod 限额 4 核） |

## Architecture

```
worker pod 的 cgroup（4 核）
└── sbx_<id>/            ← 每沙箱一个子 cgroup（由 c3-agent 建/管）
    ├── cpu.max         = 声明的 cpu_percent（含 supervisor！）
    ├── memory.high/.max= 声明的 memory_mb（high 先节流，max 兜底）
    ├── pids.max        = max_processes
    └── cgroup.procs    ← 槽位/supervisor 在 spawn 时就被写进来，之后 fork 的进程自动继承
```

谁来做：**每节点的 root 组件（c3-agent 面 B）**。它已经是唯一 root、已经按 `{sandbox_id, op}`
承接文件操作、也是 route-B 槽位的 spawn 方 —— 把"建 cgroup / 放进程 / 读用量 / 收尾"加进同一张
op 表，是权限面最小的一步。

它要的**唯一新权限**：一块 **rw 的 cgroupfs 视图**（`hostPath: /sys/fs/cgroup` → `/host-cgroup`）。
现有 root + `CHOWN/DAC_OVERRIDE` 足以在 worker pod 的 slice 下建目录、写 `cpu.max`、写
`cgroup.procs`；路径**不用猜**：读 `/proc/<worker/slot pid>/cgroup` 拿到相对路径再拼回宿主视图。

**放置进程**：route-B 槽位本来就是 agent spawn 的，在 spawn 处写 `cgroup.procs`（或
`clone3(CLONE_INTO_CGROUP)`）；进程内形态由 worker 通过一次 op 请求让 agent 放。子进程继承 ✔。

**收尾**：`cgroup.kill` + `rmdir`；孤儿由现成的 N48 那条 GC 兜底。

## Global Constraints

- **不给 worker `privileged`、也不把整棵 cgroupfs rw 挂给 worker** —— 那等于给它"节流/杀掉同族
  其它 pod"的权力，与 C3「worker 一个特权动作都不做」直接冲突。
- **agent 的新权力必须收窄**：只暴露 `cgroup.create/place/read/kill` 四个 op + 路径白名单
  （解析后路径必须落在 `kubepods*…pod<该 worker uid>.slice/<sandbox_id>` 之下，和 `e2b-maint`
  的五根白名单同一形状），不是"能写 cgroup"。
- **cgroup 不做磁盘与网络额度**：磁盘是 NAS 上的账本 + `RLIMIT_FSIZE`，网络是策略/代理，
  两者都不进来。
- **内存语义变化要写进文档**：`memory.max` 超限是 OOM kill，今天是 mmap 记账回 ENOMEM；
  建议 `memory.high` 节流 + `memory.max` 兜底，记账保留给**虚拟化**（`/proc/meminfo`、`sysinfo`）。

## 安全风险（2026-10-06 实测后写死；这是批准 Phase 1 时要一起看的）

**结论先说：它不给沙箱开新的逃逸面 —— 新增的风险全在"节点 root 组件多出来的一类权力"和"那块 rw
挂载的范围"上。**

**今天 agent 对沙箱进程是只读的（实测）**：`CapEff = 0x0b`（只有 `CHOWN|DAC_OVERRIDE|FOWNER`，
**没有 `CAP_KILL`/`CAP_SYS_PTRACE`/`CAP_SYS_ADMIN`**）；即便 uid 0，对池 uid（10000+）的
`sandlock-superv` 做 `kill -0` / `kill -TERM` 都是 **EPERM**，但 `cat /proc/<pid>/status` 可以。
⇒ cgroup 写权限把它从"能读、能在五根白名单里改文件"变成"**能节流、能 OOM、能杀**"——这是一类**新的
权力**，不是同类的加量。

**不新增的**：① 沙箱自己拿不到任何东西（它的挂载命名空间里没有 cgroupfs，写不了自己的限额；`/proc/self/cgroup`
本来就只读可看）；② pod/节点限额不被削弱（每箱 cgroup 是**嵌套**子节点，pod 的 4 核/4 GiB 仍然生效，
取二者较小）；③ 可达性不变（op 仍走 CP→agent 的 token + NetworkPolicy，worker 敲不进来）；④ 对已被
攻破的 CP 边际为零（它本来就能 `DELETE /sandboxes/{id}`）。

**真正的风险点，按严重度排**：

1. **挂载范围**：hostPath 无法指向"动态的每 pod 路径"（pod uid 每次都变），所以现实里只能挂
   `/sys/fs/cgroup/kubepods.slice`（含 kube-system 的 pod）或整棵 cgroupfs ⇒ **代码里的白名单是唯一
   收窄**，一个路径解析 bug 就等于"能节流/杀掉本节点任意 pod 的进程"。
2. **白名单必须在解析后的路径上做**（`c3_agent/priv/priv_common.c` 已经踩过这个坑：符号链接/`..` 都要先
   解析再比较），而且 **op 不接受 CP 传来的路径**：agent 自己由 `sandbox_id` + 目标 worker pod uid 拼。
3. **一个具体的 fail-open 形态**：若 worker pod slice 的 `cgroup.subtree_control` 没有下发 `cpu`/`memory`，
   子 cgroup 里写 `cpu.max` 会**不生效**（限额静默失效）⇒ 落地时必须**建完回读**（`cpu.max`/`memory.max`
   读回 + 一条自旋探针），把"限额真的生效"当成验收判据，而不是写完就算。
4. **TOCTOU**：spawn 之后再写 `cgroup.procs`，窗口里 fork 出去的进程会留在 worker 的 cgroup（**逃出限额**）
   ⇒ 用 `clone3(CLONE_INTO_CGROUP)`（fork 已经在往这个方向走）或先停住再放。
5. **不要把沙箱移出 pod slice**（换成节点级 `sandlock.slice` 虽然能把挂载收窄到那一棵，但会失去 k8s pod
   的 CPU/内存兜底，也要重做 pod 的用量记账）⇒ 保持嵌套在 worker pod 之下。
6. **fail 方向钉死 closed**：建不出 cgroup / 写不上限额 ⇒ **拒绝建箱**，绝不"无额度放行"。代价是 agent
   变成建箱的硬依赖（它本来就是：槽位 spawn 与文件操作都走它）。

**备选（不扩大任何权限）**：N82 里那条——supervisor 自记账（`getrusage(SELF)` 并进沙箱用量）+ **无条件
arm 沙箱自己的 `max_cpu`**（今天 `cpu_percent=100` 时根本不 arm，实测 4 个自旋能到 3832 mcore）。两件都在
fork 内、纯代码，覆盖"supervisor 替它花的那半"与"沙箱自己烧的那半"；代价是没有内核级精确度，`memory.max`/
`pids.max` 那两块仍走老路。**Phase 1 与它二选一，或先备选后 cgroup。**

**worker 面与沙箱面（2026-10-06 实测；这两面才是用户实际担心的）**

- **worker 面：今天对 cgroup 零写路。** `uid=65534 CapEff=0000000000000000`，`/sys/fs/cgroup` 是
  `ro` 挂载 —— `echo $$ > cgroup.procs` 与 `mkdir` 都是 **`Read-only file system`**。Phase 1 必须
  保持这一点：**写走 CP→agent**（与现成的 `POST /internal/nodes/{id}/file-op` 同一条：worker 只报
  `{sandbox_id, op}`，CP 派生目标并做归属检查，agent 执行），worker 只保留**读**（Phase 0 的计量）。
  ⚠ 要避免的形态：为了"让 worker 自己放 supervisor"而给它 rw cgroup 视图 —— 那等于把写权限放进沙箱相邻
  组件；放置应交给 agent 在 spawn 时用 `clone3(CLONE_INTO_CGROUP)`。
  **万一 worker 被攻破**：新增的是"对**本节点**同侪沙箱的**进程级**控制（节流/杀）"。**范围不变** ——
  `file-op` 那条已经有对象检查（`record.node_id != node_id` → 403），也就是说它今天就能对本节点的沙箱
  做数据级操作；新的是**种类**（进程 vs 数据），不是范围。缓解：cgroup op 复用同一条归属检查 + agent 的
  解析后白名单 + 目标由 CP 派生（worker 不传路径、不传 uid —— 与 file-op 的硬规则 1/3 相同）。
- **沙箱面：零可见、零可达（实测）。** 沙箱里 `cat /proc/self/cgroup` → **EACCES**（`/proc` 是中介
  合成的，`self/cgroup` 不在白名单）；`ls /sys` → **EACCES**；`grep -c cgroup /proc/self/mountinfo`
  → **0**；`ls /sys/fs/cgroup` → **ENOENT**。⇒ 每沙箱 cgroup **不给沙箱任何新信息、也拿不到任何句柄**，
  "目录用 sandbox_id 还是池 uid 命名"这个问题**不存在**。
  逃不出限额：它没有 cgroupfs 的任何 fd，写不了 `cgroup.procs`/`cpu.max`；它 spawn 的一切继承同一
  cgroup；唯一能"逃"的是放置前的 TOCTOU（用 `clone3(CLONE_INTO_CGROUP)` 关掉）。
  不削弱现有边界：每箱 cgroup **嵌套在 worker pod 之下** ⇒ pod 的 4 核/4 GiB 照旧（取二者较小）。
  对沙箱的**收益**：每箱 `cpu.max` 把 N82 那类"邻居被吵"变成"花自己的额度" —— 这是沙箱面的安全**改善**
  （跨租户公平/DoS），不是新增风险。
  对沙箱的**功能**变化（非安全）：`pids.max` → fork `EAGAIN`；`memory.max` → OOM kill（今天 mmap 记账
  回 ENOMEM）；`cpu.max` → CFS 100 ms 周期节流（比通知限流那 860 ms 的一秒悬崖平滑得多）。

## Tasks

### Phase 0 —— 计量（**✅ 已上线 2026-10-06**）

落点在 **worker**（不是 agent）：worker 自己的 `/proc` 就能看到整棵树（小 pid、同一 pooled uid），
复用 E9.1 的按 uid 采样即可 —— 零新权限。

- `_cpu_activity_round` 每轮为**每个运行中的沙箱**记一次 `record_cpu_percent(sandbox_id, pct)`
  （0% 也是读数）；连续 3 轮超过声明额度打具名 WARN。
- 心跳带 `sandboxCpu`（与 `sandboxActivity`/`sandboxDiskUsage` 同一分工：worker 测、CP 记）。
- CP：`SandboxRecord.measured_cpu_percent`（`to_dict`/`from_dict` 都要带上）、
  `apply_cpu_report`（节点守卫 + 校验 + **save**）、`GET /internal/nodes/{id}/sandboxes` 的并列
  `sandboxes[].measuredCpuPercent`（`sandboxIDs` 不动）。
- 现场：4 个自旋 ⇒ `measuredCpuPercent = 375.5`（声明 100%）；日志
  `cpu over allowance: sandbox … measured 382% of a core for 3 rounds`。

### Phase 1 —— 每沙箱 cgroup + `cpu.max`（**待拍：这是权限扩张**）

- [ ] agent 面 B 加 rw cgroupfs 视图（`hostPath /sys/fs/cgroup` → `/host-cgroup`）+ 四个 op
      （创建带限额 / 放进程 / 读 `cpu.stat` / `cgroup.kill`）+ 路径白名单。
- [ ] 槽位 spawn 时把 supervisor 放进 `<worker slice>/<sandbox_id>`；进程内形态走一次 op。
- [ ] `cpu.max` = 声明的 `cpu_percent`（**含 supervisor**，这正是重点）。
- [ ] 验收（三条，缺一不可）：① 沙箱内 4 个自旋被压在额度内（今天 3832 mcore / 额度 100 ⇒ 应
      ≈1000 mcore）；② 同节点第二个沙箱的 `command_rtt` 不掉速；③ **把
      `E2B_SANDBOX_NOTIFY_RATE_LIMIT` 关掉之后**，`open+close` 洪泛仍落在额度里（今天关掉是
      18149 op/s / 1.02 核）。
- [ ] 通过后：限流器降级为冗余背板（可保留一个很高的值，或直接退役）。

### Phase 2 —— 内存与进程数

- [ ] `memory.high`/`memory.max`/`pids.max` 接上；把 mmap 族与（argv-safety 允许时的）clone 族
      从通知表退掉，走 `path_surface` 账本那套"从已中介移出 + 写明理由"的流程。

### Phase 3（可选）

- [ ] `io.max` / `cpu.weight` 做公平分担；`memory.events` 接进指标。

## 本计划不做

| 项 | 裁定 |
|---|---|
| worker `privileged: true` / 整棵 cgroupfs rw 挂给 worker | **不做**。等于把"节流/杀同族 pod"的权力交给沙箱相邻组件。 |
| 用 cgroup 做磁盘/网络额度 | **不做**。磁盘有账本 + `RLIMIT_FSIZE`，网络有策略与代理；分开管。 |
| 把 supervisor 留在沙箱额度之外、继续用通知限流兜底 | **不做**。那正是 N82 测出来的"替人记账"，且一秒级悬崖不可接受。 |
| 自记账（supervisor 读 `getrusage` 并进沙箱用量） | **本轮不做**，作为 Phase 1 的备选：改动小、不引新权限，但只在**由 supervisor 执行**的路径上成立（进程内形态的记账在 worker 里），且不覆盖"沙箱自己的进程直接烧 CPU"那一半 —— 那半今天靠 `max_cpu`，而它默认没 arm。 |
