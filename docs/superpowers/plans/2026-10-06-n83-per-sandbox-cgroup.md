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
