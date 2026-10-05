# N81：stat 族彻底放行（N79 选项 ②）实施计划

**Goal:** 让 `stat`/`access` 不再进 seccomp 通知表 —— `stat` 从 26 µs 回到内核的 ~1.2 µs，
限流那套天花板（5000/s 通用、20000/s stat 单列）对元数据调用彻底不再适用。

**立项时的前置已经消失（本次实测）**：N79 说"放行的前置是 `/proc` 的 stat 语义"，指的是
**模拟根**形态 —— 那时沙箱的内核根就是宿主 `/`，`stat /proc/<宿主pid>` 会撞宿主 pid。N14 S5
把模拟根退役之后，真根是唯一形态，沙箱的 `/proc` 变成它**自己文件系统里的空目录**：

| 判据 | 读数 | 出处 |
|---|---|---|
| `/proc` 是不是独立挂载 | `stat /proc` 与 `stat /` 的 `st_dev` **相同**（66306 == 66306） | 线上 `0.1.0-1027`，探针 `deploy/scripts/acceptance/probe_n81_proc_stat_shape.py` |
| 内核眼里 `/proc` 里有什么 | `/proc/uptime`、`/proc/version`、`/proc/meminfo`、`/proc/cpuinfo` 全 **ENOENT** | 同上（这四条是非数字路径，处理器本来就 `Continue` ⇒ **是内核在答**） |
| 对照：数字 pid | `stat /proc/1` → **EACCES**（中介的拒绝，不是内核的答案） | 同上 |
| 磁盘上的真相 | worker 上**每一个**已解包 rootfs 的 `rootfs/proc`、`rootfs/sys` 都是**空目录**（`drwxr-xr-x 2 … 6`） | `kubectl exec e2b-worker-0 -- ls -ld /var/lib/e2b-images/*/rootfs/{proc,sys}` |
| 合成根（pure 默认档） | 骨架只绑 `/usr /bin /sbin /lib /lib64 /opt`；`proc` 在 `_SYNTHETIC_ROOTFS_SKELETON_DIRS` 里（空目录），`sys` **根本不在骨架里** | `envd_service/executors/sandlock.py:468-498` |

**Architecture:** 把 stat 族按"内核能不能自己答对"拆开：

- **metadata 类**（`newfstatat`/`statx`/`faccessat`/`faccessat2` + 旧 ABI `stat`/`lstat`/`access`）
  —— 真根下内核解析到的就是同一个文件，中介的 `stat_and_write` 只是把同样的数字重写一遍。
  **放行**（移出 notify 表 + 不注册处理器）。
- **link 类**（`readlinkat` + 旧 ABI `readlink`）—— **保留**。`handle_chroot_readlink` 把
  `/proc/self` 规范化成 `/proc/<宿主 pid>` 后在**宿主**的 procfs 上代读，并按 `processes` 做
  per-pid 门；`/proc/self/exe`、`/proc/self/fd/N` 今天就是靠它工作的（现场实测
  `readlink /proc/self/exe` → `/usr/local/bin/python3`）。
- **只在"沙箱有自己的真根"时放行**：`chroot.is_some()`，而 N14 S5 让 `chroot && !real_root`
  变成构造期具名拒绝（`sandbox.rs:2164`）⇒ 这是一个已经成立的隐式不变量，不必新增开关。
  没有根的形态（E5.1 进程内形态、fork 的库使用者）**保持今天的拦截** —— 那种形态下沙箱的
  `/proc` 是**容器自己的** procfs，数字 pid 真的会撞上容器的进程表。
- **放行的前提用构造期守卫钉住，不靠文档约定**：建箱时（父进程侧，装过滤器之前）检查
  `statfs(<root>/proc).f_type != PROC_SUPER_MAGIC`，不是普通目录就**具名拒绝**。这样将来
  任何人往 rootfs 里 bind 宿主 `/proc`，都在建箱那一刻被点名，而不是悄悄泄露。

**Tech Stack:** Rust（`crates/sandlock-core`：`seccomp_plan.rs` / `seccomp/dispatch.rs` /
`procfs.rs` / `sys/path_surface.rs`）、pytest（E2B 侧）、Docker（`sandlock-dev:latest` 跑 fork 门禁）、
k0s（现场验收）。

**Spec:** `docs/open-issues.md` 的 N79 行（选项 ②）与本次新增的 N81 行；
`docs/benchmarks.md` §③；探针 `deploy/scripts/acceptance/probe_n81_proc_stat_shape.py`。

## Global Constraints

- **`readlinkat`/`readlink` 不许动**：动了就断 `/proc/self/exe`、`/proc/self/fd/N`，而且那是
  一条**服务**路径不是拒绝路径，没有内核替代品。
- **只在有真根的形态放行**：`chroot.is_some()`。无根形态（E5.1、库使用者）必须是今天的形状。
- **守卫先于放行**：`<root>/proc` 是 procfs 挂载 ⇒ 建箱具名拒绝；这条守卫进 fork 的门禁。
- **`path_surface.rs` 的账本要同步**：`MEDIATED_PATH_SYSCALLS` 与 `chroot_path_syscalls()` 是双向
  pin（`mediated_set_matches_the_ledger`）。把 7 个名字从"已中介"挪到"**有意不中介，理由在此**"
  是这次改动的一部分 —— 那个账本存在的意义就是逼出这个决定，不是噪声。
- **Landlock 不动**：`open`/`exec`/`write` 的准入继续由它管；这次只放开**元数据**。
- 基线：fork 门禁八相位（core_lib 932 / core_integ 570 / ffi 104 / cli 98 / supervise 57 /
  supervise_cost 3 / cli_build 0 / python 466）、E2B `tests/unit` 3 条 macOS 固有红。

## Review Focus

按"最可能咬人"排序：

1. **放行范围漏 gate**：把"有根"这个前提丢了，就会连 E5.1 形态一起放行 —— 那个形态的 `/proc`
   是容器的 procfs。判据：plan 级单测（有根 ⇒ 不在表里；无根 + `pid_ns` ⇒ 在表里）。
2. **守卫写成了约定**：N79 的教训是"前提消失了但文档还在说"。守卫必须是构造期、具名、拒绝，
   而且有一条 RED→GREEN 的用例（把 `<root>/proc` 换成 procfs 挂载 ⇒ 建箱失败并点名）。
3. **顺手把 readlink 也删了**：`/proc/self/exe` 是**能跑通**的路径，删掉就是功能回归。
4. **账本没跟上**：`path_surface` 的 pin 会红；这不是噪声而是设计要求（每个带路径 syscall 要么
   被中介、要么有理由）。消一条要同步改 pin。
5. **N79 的 `notify_rate_limit_stat` 变成空类**：stat 不再进表 ⇒ 那 20000/s 的窗口没有成员。
   留着是"设了没反应"的文档陷阱，删掉是策略线的兼容面改动。**建议随之退役**（N79 的动机被 N81
   取代），并在 N79 行写明"由 N81 取代"。
6. **`/proc` 的 errno 变化**：`stat /proc/<宿主pid>` 从 `EACCES` → `ENOENT`。两者都不泄露，
   但那是一条**已有探针的判据**（`probe_n79_proc_stat_denied.py` 现在断言 EACCES），必须一起改，
   否则下次跑会红得莫名其妙。

## Tasks

### Task 1: fork —— 拆名单 + 按形态 gate + 构造期守卫

**Files:**
- Modify: `third_party/sandlock/crates/sandlock-core/src/seccomp_plan.rs`
  （`stat_family_syscalls()` 拆成 metadata/link 两组；`chroot_path_syscalls()` 去掉 metadata 成员；
  `pid_ns_procfs_stat_syscalls()` 只在**无根**形态保留）
- Modify: `crates/sandlock-core/src/resolved.rs` / `seccomp/notif.rs`（`real_root` 进 features/NotifPolicy）
- Test: `seccomp_plan.rs` 的 `#[cfg(test)]`（计划级三条：有根不带 metadata、无根带、link 永远在）

- [ ] **Step 1: 写失败用例**（三条，精确相等断言，不用 contains）
- [ ] **Step 2: 跑，确认失败**
- [ ] **Step 3: 实现**（`features.real_root && features.chroot` ⇒ 不放 metadata）
- [ ] **Step 4: 守卫**：`<root>/proc` 是 procfs ⇒ 建箱具名拒绝（含 RED→GREEN 一条）
- [ ] **Step 5: commit（fork）**

### Task 2: fork —— 删掉 metadata 的处理器与账本

**Files:**
- Modify: `crates/sandlock-core/src/seccomp/dispatch.rs`（不再注册 stat/access 的 chroot 处理器；
  pid_ns 的 stat-family 注册整段删除）
- Modify: `crates/sandlock-core/src/procfs.rs`（`handle_proc_stat_family` 删除，它的 readlink 半边
  本来就由 `handle_chroot_readlink` 承担）
- Modify: `crates/sandlock-core/src/sys/path_surface.rs`（账本：7 个名字移到"有意不中介 + 理由"）
- Test: 上述两处的单测 + `path_surface` 的 pin

- [ ] **Step 1..4**: 删代码 ⇒ 账本红 ⇒ 改账本 ⇒ 全绿
- [ ] **Step 5: commit（fork）**

### Task 3: N79 的 stat 单列预算退役（建议）

**Files:** `crates/sandlock-core/src/seccomp/notif.rs`（`NotifyBudget.stat`）、
`crates/sandlock-core/src/sandbox/builder.rs`、`sandbox.rs`、`sandlock-supervise/src/policy.rs`、
FFI + 头、Python SDK、`envd_service/config.py`、`route_b.py`、`tests/unit/test_notify_rate_budget.py`

- [ ] 二选一（**需要拍**）：
  - **A（建议）退役**：字段/环境变量/`_HANDLED_FIELDS`/策略线键集一起删，N79 行改"由 N81 取代"。
  - **B 留空**：保留字段，但钉一条"stat 类今天没有成员"的用例 + 文档写明它不生效。

### Task 4: 现场验收

- [ ] `deploy/scripts/acceptance/probe_n81_proc_stat_shape.py` → `PROC-SHAPE OK`（**新 pin**）
- [ ] `probe_n79_proc_stat_denied.py` 改成断言 **ENOENT**（同一件事的 errno 换了）
- [ ] **越界不可达的钉子**（新增，承重）：从沙箱里对 worker 自己的路径做 `stat`/`access`
      —— 至少 `/etc/shadow`、`/var/lib/e2b-images`、`/var/lib/e2b-sandboxes/state`、
      `/var/lib/e2b/workspaces`。判据不只是"这些路径不成立"，而是**更强的那条不变量**：
      **凡是沙箱 `stat` 得成功的路径，`st_dev` 必须等于它自己根目录（`/`）的 `st_dev`**。
      今天（改动前）的基线读数已经量过：`/etc/os-release`（dev 66306）、`/etc/shadow`
      （dev 66306，**那是镜像自己的那份**，size 474/0640）、三条 worker 路径 ENOENT、
      `/proc/1` EACCES —— 宿主自己的文件在**另一个 dev** 上，一旦有一天它进了沙箱的挂载命名空间，
      这条判据会当场变红。反向对照（`/etc/os-release` 必须成功）写在同一支探针里，
      照 `probe_n81_proc_stat_shape.py` 的体例
- [ ] `lightweight_metrics_probe.py`：`stat` p50 应从 **25.7/26.4 µs** 掉到 **~1–2 µs**
      （裸形态 §2.4.10.2 的 1.2 µs 量级）
- [ ] `tmp/stat_stall_hunt.py`：开口 ~37k/s 时应**跑满**（不再被 20000/s 压住），且不再有每秒一次的长停顿
- [ ] `MULTI-NODE SMOKE` / `DEPLOYMENT SMOKE` / `checkpoint_acceptance.py`

### Task 5: 记录

- [ ] `docs/open-issues.md`：N81 新行 + N79 行改"由 N81 取代"
- [ ] `docs/benchmarks.md` §③：新的 p50 与"限流不再作用于 stat"
- [ ] `docs/isolation-boundaries.md` / `security-architecture.md`：`/proc` 的 stat 关口改成
      "靠 rootfs 的空 `/proc` + 构造期守卫"，并写清 errno 从 EACCES 变 ENOENT
- [ ] `docs/deploy-clusters.md`：发版记录

---

## 放行会不会引入新的漏点（2026-10-06 增补）

今天的拦截不只是"拒 `/proc`"，它还是**元数据的准入闸** —— Landlock 管不到 `stat`（它只有 open/read 的权限位）。所以判据只有一条：**有没有路径是"沙箱看得见、但不在可读集里"**。

`ChrootCtx::can_read` 的规则（`crates/sandlock-core/src/chroot/dispatch.rs:192-202`）是
`!is_denied_any(vp) && (is_mounted(vp) || readable 前缀命中 || writable 前缀命中)`，
而 `is_mounted` 是**前缀**匹配（挂载点以下的任何路径都算"挂着的、可读"）。

| 沙箱可达的东西 | 今天 `stat` | 放行后 | 评价 |
|---|---|---|---|
| image rootfs（`fs_readable` 含 `"/"`） | 允许，**中介代答**（数值与内核相同） | 内核答同值 | 无差 |
| 声明的挂载（workspace / 卷 / 六个 `/dev` 节点；合成根的**整棵 `/dev`**） | 允许（`is_mounted` 前缀命中） | 内核答同值 | 无差 |
| `/proc/<n>`（宿主 pid） | **EACCES**（pid_ns 处理器） | **ENOENT**（空 `/proc`） | 不泄露；errno 变了 |
| `/sys/*`、`/proc/kcore`（`fs_denied`） | **EACCES** | `/proc` 空 ⇒ ENOENT；`/sys` 是**镜像自己的空目录** | 不泄露宿主数据 |
| 合成根里未绑定的骨架目录（`/var`、`/run`…） | 允许（它们本身就是空目录） | 内核答同值 | 无差 |
| 宿主绑进来的 `/usr /bin /sbin /lib /opt` | 允许（`readable` 命中；`/sbin`→`usr/sbin`、`/bin`→`usr/bin`、`/lib`→`usr/lib` 都是软链，`/lib64` 在 worker 镜像里**不存在**） | 内核答同值 | 无差 |

⇒ 两个形态里，**"可达但不在可读集"的路径只有 `fs_denied` 那两条**，而它们在真根下都是沙箱
自己 rootfs 里的空东西。`/dev` 整棵树看着像"绑了却没声明可读"，其实 `fs_mount` 里声明了
`/dev`，`is_mounted` 前缀命中 ⇒ **今天就已经是可读的**。

**三条结构性前提**（这才是真正承重的东西，实现里要钉住，不能只写在文档里）：

1. **沙箱的挂载命名空间里只有它自己的 rootfs + 声明的挂载** —— 依据是真根的
   `pivot_root` + 策略挂载。这是"内核答的就是它自己的东西"的全部来源。
2. **没有指向 root 之外的目录 fd** —— 否则 `fstatat(dirfd, name)` 会绕开一切
   （`AT_EMPTY_PATH` 的 `fstat` 今天就已经走内核）。这条今天由 SL-4/SL-11 的 fd 表钉子看着；
   放行后它从"锦上添花"变成**承重**。
3. **`<root>/proc` 不是 procfs 挂载** —— 构造期守卫（Task 1 Step 4）。

**两处不是漏点、但行为会变**：

- `faccessat` 今天**只判存在就回 0**（忽略 `mode`，见 `handle_chroot_stat` 末段），放行后是内核的
  真权限判断 ⇒ 变正确，但依赖旧行为的脚本会改走向（这是收紧，不是放松）。
- `/proc/<n>` 的 errno 从 `EACCES` 变 `ENOENT`（`probe_n79_proc_stat_denied.py` 要跟着改）。

**结论**：在生产（image rootfs）形态**不引入新的可达漏点**；纯/合成根形态同理（前提 1、2 同样
成立）。代价是把"能不能看见元数据"完全押在前提 1、2 上 —— 所以 Task 4 要多一条**"越界不可达"的
钉子**：从沙箱里对 worker / 平台自己的路径（例如 worker 的 `/etc/shadow`、`/var/lib/e2b*`）做
`stat`/`access`，必须全部不成立。

---

## 本计划不做（已裁定，理由记进 ledger）

| 项 | 裁定 |
|---|---|
| path-aware 的 LSM/BPF（用内核侧钩子拒 `stat /proc/<pid>`） | **本轮不做**。它是"有根放行"之外的**第二方案**：需要内核 `CONFIG_BPF_LSM` + 节点上一个能加载 BPF 的组件（worker 是 uid 65534、c3-agent 的 cap 集里没有 `CAP_BPF`），本部署**未评估**。真正需要它的是"没有自己 rootfs"的形态 —— 那种形态本次保持原样。 |
| 连 `readlinkat` 一起放行 | **不做**。它是服务路径（`/proc/self/exe`、`/proc/self/fd/N` 由它代读），不是拒绝路径。 |
| 给无根形态（E5.1 / 库使用者）也放行 | **不做**。那种形态的 `/proc` 就是容器的 procfs，数字 pid 真的会撞容器进程表。 |
| 动 Landlock | **不做**。这次只放开元数据；`open`/`exec`/`write` 的准入不动。 |
| 顺手把 `fs_denied=["/proc/kcore","/sys"]` 删掉 | **不做**。它们放行后落到内核实答（空 `/proc`、rootfs 自己的 `/sys`），保留这两个 deny 是零成本的第二道说明；清理由独立一条做。 |
