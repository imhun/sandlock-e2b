# N88 选项 ②：让两家自己看门，`close` 彻底离开通知表（fix-forward）实施计划

**Goal:** 把 N88 选项 ① 那行条件（`if features.inbound_port_map { nrs.push(SYS_close) }`）
删掉 —— 也就是让 **epoll readiness** 与 **inbound 端口映射** 都不再依赖"每次 `close` 来一条通知"，
而各自在**使用时**自校验/惰性撤。做完之后，通知表里就没有 `close` 了（普通形状现在也没有）。

**为什么不能照抄 netlink 的做法了事**：netlink cookie 是"一张 `(tgid, fd)` 表 + 一个可自证的身份"，
所以改成用时校验（`/proc/<pid>/fd/<fd>` 的 identity）是纯机械的。N88 的这两家不同：

- **inbound 映射**（`network/inbound.rs`）：`close` 是**语义事件** —— 代码注释原文 *"close() drops the
  mapping (closing the host listener) when the listening socket closes"*。它按 **socket inode** 记
  `ns.inbound`；没有通知之后，"宿主 listener 什么时候释放"必须重新定义，而不是原地换个校验。
- **readiness**（`network/readiness.rs`）：`epoll_registrations` 按 **(pid, fd)** 记，与 netlink 同形
  ⇒ 这半边可以照抄用时校验，但它**必须**与 inbound 的新契约一起想（它服务的就是映射端口的 accept）。

**Architecture（两半分开做）**

1. **readiness：用时校验（照抄 netlink 的形状）**
   - `epoll_registrations` 的值从"占位"改成 **fd 的 identity**（注入/登记时读一次
     `readlink("/proc/<pid>/fd/<fd>")`）。
   - `handle_epoll_ctl` / `handle_epoll_wait` / `handle_inbound_accept` 命中条目后**复核** identity：
     不符（fd 关了、号被复用、进程没了）就删条目并当普通 fd 处理。
   - 表仍有界：键是 fd 号，来自沙箱自己的 `RLIMIT_NOFILE`（与 netlink 同一条论证）。
   - **实测踩点（2026-10-07，动手前侦察）**：`readlink("/proc/<pid>/fd/<epfd>")` **不能**当身份 ——
     epoll fd 是 anon inode，每个 epoll fd 的 readlink 都是同一个字符串 `anon_inode:[eventpoll]`；
     身份要用 **inode 号**（`std::fs::metadata("/proc/<pid>/fd/<fd>")` 的 `ino()`，`MetadataExt`），
     它同时适用于 socket（netlink 那条路的身份也可以统一成这个）与 anon inode。
   - `EpollRegistration.mapped_ino` 是**宿主侧 listener** 的 inode（不是被监视 fd 的身份），
     所以"fd 复用"的守卫要**新增**一条 epfd 身份记录（例如 `NetworkState` 加
     `epoll_identity: HashMap<(u32, i32), Option<u64>>`，ADD/MOD 时写、DEL/close 清、
     `handle_epoll_wait` 取快照前复核），不要试图复用 `mapped_ino`。
   - **钉子的落点**：`handle_epoll_*` 需要 `SupervisorCtx` 与真实 epoll fd，纯单测不好搭 ⇒
     这条钉子大概率要落在 `tests/integration`（带映射端口的沙箱：注册 → `close(epfd)` →
     用同一个 fd 号做别的事 → 不得被合成接管），与 N88 的"带映射形状车道读数"同一次做。
2. **inbound：把"close 时撤"换成两种可接受的时点之一**（**这一步要人拍**，见下）
   - **(a) 惰性替换**：`ns.inbound` 只在 `listen()`（映射端口）时写入/替换；旧条目由"**用时复核**"
     淘汰 —— 复核 = 那个 inode 是否还是**活的***沙箱侧* socket（`socket_ino(dup_fd_from_pid(...))`
     与条目里的 inode 比对，`handle_inbound_close` 现在就是这么拿 inode 的）。
     语义差别：沙箱关掉监听 socket 后，**宿主 listener 会多活一会儿**（直到沙箱再 listen 同端口、
     或沙箱退出）—— 必须写进 `docs/` 的语义节，并由钉子表达。
   - **(b) 主动发现**：用一条有界的周期扫描（或 pidfd/inotify 类机制）发现"沙箱侧监听 fd 没了"就撤。
     代价是一条后台循环；收益是宿主端口占用时间回到今天的行为。
   - **建议 (a)**：没有后台循环、没有新机制，代价只是"宿主端口占得比过去久"，而该端口是平台自己
     分配给这个沙箱的（`50005+`），不会与别人抢。
3. **删掉 ① 那行条件**，并**保留** `close` 在任何表里都不出现的状态（`NETLINK_NOTIF_SYSCALLS` 已经
   不含它）。

**Tech Stack:** Rust（`seccomp_plan.rs`、`network/{inbound,readiness}.rs`）、`sandlock-dev` 门禁、
`build-sandlock-wheels.sh`、本地 compose 车道（`off` 车道读阶梯、`required` 车道读语义）、k0s 现场。

**Spec:** `docs/open-issues.md` N88（① 的落地与残留）、N82 候选 ①；本次读数见 §7.53/§7.54。

## 判据（做之前先写钉子，取 RED）

1. **close 释放宿主 listener**（N88 要的第一条语义钉子）：沙箱在映射端口 `listen()` → 宿主侧
   `connect()` 通 → 沙箱 `close()` → 宿主侧再次 `connect()` 必须**拒绝**。今天（① 在位）它绿；
   把 ① 那行删掉、其它不动 → **必须红**（这就是 ② 要修的）。
2. **fd 复用不被陈旧注册服务**（第二条）：`epoll_ctl(ADD)` 注册 → `close(epfd)` → 用同一个 fd 号开
   一个普通 fd → `epoll_wait/ctl` 不得被 readiness 合成接管。同 `test_netlink_virt::a_reused_fd_number_is_not_virtualized`
   的形状。
3. **阶梯不回归**：`probe_n82_traced_syscall_costs.py --op openclose`（`off` 车道 + 限流 5000）——
   做完 ② 之后，**带映射**的沙箱也应当回到 **1 条通知/op**（≈5080），而不是 ① 的 ~2570。
4. **inbound 语义的读数**：按 (a) 落地时，钉子 1 要改成"宿主 listener 在**再 listen/沙箱退出**时才释放"，
   并把旧行为与新行为都记在 N88 行里（口径变更必须留两次读数）。

## 任务

- [x] **T1**：写判据 1/2 两枚钉子，在**今天的树**上跑 —— 1 应绿（① 在位）、2 应红（readiness 现在
  只有 ① 的通知在兜底？先读一遍现状：若 2 已绿，说明还有别的兜底，先查清再动）。
  **实测与计划的配对相反**：`close` 的兜底就是 `if policy.inbound_port_map` 下注册的
  `handle_epoll_close`，所以"fd 复用"那枚在 ① 在位时是**绿**的；而 inbound 那枚按 ② 的新契约写
  （close 不释放宿主 listener、再 listen 要接管）在 ① 在位时就是**红**的。两枚都写在
  `test_net_isolate.rs`（走带映射端口的真实沙箱，不是单测）：`n88_a_reused_epoll_fd_number_is_not_synthesized`、
  `n88_the_mapping_outlives_close_and_serves_a_relistened_socket`。
- [x] **T2**：readiness 改成用时校验，T1 的第 2 枚钉子转绿 —— **实现换成了"读内核的 fdinfo"**（§0）。
- [x] **T3**：inbound 按 **(a) 惰性替换 + 用时复核** 落地，T1 第 1 枚钉子按新契约改写并绿 —— 途中
  挖出"映射查的是 real 端口而不是 virtual 端口"这个既有缺陷（§0.3）。
- [x] **T4**：删掉 ① 的条件行，跑 fork 门禁（十相位；`cli` 相位的环境红按 N87 的口径单独记）。
  **十一档全绿、逐档与基线一致**：core_lib 944 / core_integ 577 / ffi 104 / cli 100 / supervise 57 /
  supervise_cost 3 / cli_build 0 / python 465（`deploy/scripts/fork-gate.sh`）+ oci 157 /
  supervise_root 4 / mediation_2uid 9（`--oci-root` 等三个 root 相位要**以 root**跑，`fork-gate.sh`
  按设计会降到 65534 拒绝它们，改成同镜像里手工跑；途中一次自造的假红：`mediation_2uid` 的
  `setcap` 因为我的 runner 少给 `/usr/sbin` 而 NotFound，补回 PATH 后 9/9）。
- [x] **T5**：重建 wheel + 本地车道两条读数（普通形状 + 带映射形状；后者需要一条 net_isolation 车道）
  → 发版 → 线上复跑 9/9 与 `--op openclose` 两档。
  **轮子**：`wheels/fork/` 重建并钉到 `97718d8`（`SHA256SUMS.supervise` 的 HEAD 与两份拷贝逐字核对）。
  **本地（`-p n88b`，`off` 车道 + 出厂默认 5000，`--op openclose --seconds 40`）**：带映射形状
  BEFORE（① wheel）**2541 op/s、40/51 轮** → AFTER **5038 op/s、40/101 轮**；普通形状（不设
  `E2B_PORT_MAPPINGS`）**5039 op/s**（§7.54 的 5075 同形）。覆盖文件 `tmp/n88/override{,-mapped,
  -mapped-before}.yml`。
  **发版**：`0.1.0-1138-g50cf84f-20261007-232510`（23:25:10 构建；第一批除 worker 的 24 份 →
  23:27:19 CP+agent 收敛 → 第二批 worker → **23:27:35** 两 pod 收敛 → 幂等重放 + 预热
  `cached=true`），过程见 `docs/deploy-clusters.md` §7.55。
  **线上**：`cgroup_acceptance.py` **`ok: true`，9/9，204.3 s**；第 ③ 条单跑 **29790 op/s、0/596
  轮停顿**（① 那版 29019 / 0/581）、四路并发 24511 op/s、峰值 1.063 核、`nr_throttled +181`；
  两台 worker `E2B_SANDBOX_CGROUP=required` 且 `E2B_SANDBOX_NOTIFY_RATE_LIMIT` **unset**。
  （线上没有 `E2B_PORT_MAPPINGS`，所以那一跑量的是"普通形状无回归"；② 的收益形状是带映射的，
  读数在本地那一对。**2026-10-08 补齐**：在 worker pod 里直接建带映射的沙箱、关掉 bind 注入
  （`deploy/scripts/acceptance/probe_inbound_readiness.py`），在 arm64/6.12 上走完整条 readiness 路
  —— 中位 **81.4 ms**（同机 bind 注入 0.4 ms），沙箱自己的 epoll fdinfo 也长成 parser 假设的样子；
  顺带量到 pod 的 seccomp profile 没有 `unshare`（`sandbox_shape_matrix.py`）⇒ 这个镜像下
  `net_isolation` 必须与 `pid_ns` 成对。读数全文见 §7.55 的"补读数"与 §2.4.7。）

## 实施记录（2026-10-07）

### 0. 侦察结论更正：epoll fd **没有**可用的 identity —— 改成读内核自己的表

动手前那条"用 `metadata("/proc/<pid>/fd/<fd>").ino()` 当 epoll fd 的身份"是**错的**。dev 容器实测
（`sandlock-dev-f17`，Linux 7.0.14-orbstack）：

```
readlink1 anon_inode:[eventpoll]   readlink2 anon_inode:[eventpoll]      # 同一个字符串
fstat ino 3087 3087                # 两个 epoll 实例共用**同一个 inode**
kcmp(e1,e2) = -1 EPERM             # 非特权；特权下 EINVAL（CONFIG_CHECKPOINT_RESTORE 不可用）
```

也就是说 anon inode 的 `st_ino` 是全内核共享的一个数（3087），`kcmp` 这条路也不在。既然内核已经把
"这个 epoll 注册了哪些 fd"放在 `/proc/<pid>/fdinfo/<epfd>` 里（`tfd: %8d events: %8x data: %16llx
pos:%lli ino:%lx sdev:%x`，2.6.28 起稳定），认证的对象就从"epoll 的身份"换成**内核自己的注册表**：

- `epoll_wait` 时读 fdinfo，按 `tfd:` 行重建注册（fd / events / data / 被监视文件的 ino）；
- 只对"ino 命中 `ns.inbound`（或 fdinfo 没印 ino）"的**候选**做 dup+端口复核，其余按普通 fd 处理；
- 一条候选都不是 ⇒ `Continue`，交给内核 —— 闭合的 fd 给 EBADF、非 epoll fd 给 EINVAL，正是钉子要的读数；
- 于是 supervisor 侧**没有任何 epoll 状态**：`epoll_ctl`、`close` 一起离开通知表，
  `NetworkState::epoll_registrations` 与 `EpollRegistration` 整块删掉。

比"给 epoll fd 加一条 identity 记录"更省、更准：不复用 `mapped_ino`（那是**宿主 listener** 的 inode），
也没有"校验与合成之间兄弟线程复用 fd"的那条窗口 —— 内核的表就是那一刻的真值。

### 0.2 RED/GREEN 读数（`tmp/n88/`；每步都是同一条断言，精确匹配）

| 步骤 | 树 | 钉子 A（fd 复用） | 钉子 B（映射生命周期） |
|---|---|---|---|
| T1 | ① 在位（`5eda94d`） | **绿**（close 通知把注册清掉了） | **红**：`the mapping must outlive the sandbox listener's close`（close 把宿主 listener 拆了） |
| T4 先删 ① | ① 行删掉、其它不动 | **红**：`reused_epoll_wait_rc=0 errno=0`（期望 `-1 errno=22`） | **红**：再 listen 的那只 socket 拿不到宿主连接（accept 永远不被服务） |
| T2+T3 | 本计划的实现 | **绿** | **绿** |

### 0.3 途中挖出的既有缺陷：inbound 查的是 **real** 端口，不是 virtual 端口

`handle_listen` 原来用 `local_port(dup)`（real 端口）去查 `ns.inbound_map`。而 `port_remap::handle_bind`
在 `bind()` 撞 EADDRINUSE 时会**用 port 0 重试**并把结果记成 virtual→real 映射 —— 一个沙箱里
"关掉监听再重新 bind 同一个映射端口"恰好会撞上：宿主 listener 的 eager worker 持有沙箱监听 socket 的
dup（也让沙箱侧那个端口继续被占），于是第二次 `bind(P)` 拿到的是内核另选的一个真实端口，映射查不到
（实测 `sandbox_port=32959` → 第二次 `local_port` = `40061`，`host_port=None` ⇒ 这次 listen 建不出映射）。
修法是用 `PortMap::get_virtual(real).unwrap_or(real)` 翻回沙箱自己认的那个端口（`live_sandbox_port`），
accept / readiness 两处的用时复核也走同一个函数。

### 0.4 inbound 的落地形态（option (a)）

- `close` 不再进表 ⇒ 映射**跨过**沙箱关闭监听 socket 存活；
- 再 `listen()` 同一个映射端口（inode 不同）⇒ 先淘汰旧条目（放掉旧宿主 listener 的 fd），再建新的；
  旧 worker 还攥着宿主 listener 的 dup 最多一个 poll slice（2 s），所以重建走**有界 EADDRINUSE 重试**
  （≤5 s），并且整段放在 `defer` 里，不占通知循环；
- 每个使用点（listen/accept/poll/epoll_wait）都复核"活着的 socket 的 virtual 端口 == 条目记的
  `sandbox_port`"：inode 被内核回收时不会认错。

## 风险与回退

- **风险集中在 inbound 的语义变更**（(a) 让宿主端口占得比过去久）。回退杆：把 ① 那一行
  `if features.inbound_port_map { nrs.push(SYS_close) }` 加回 `seccomp_plan`（= 回到 N88 ① 的形状），
  或整条 `close` 加回 `NETLINK_NOTIF_SYSCALLS`（③，最保守）。
- **TOCTOU 说明**：readiness 的用时校验与 netlink 同形 —— 校验与 supervisor 的合成答复之间，兄弟线程
  可以关掉并复用 fd；那条竞态是应用自身的竞态（POSIX 下"关掉 fd 号的同时使用它"本来就没有定义），
  netlink 那一版已按同样口径记录，这里沿用同一句话，不再新开一轮论证。
- **不改的东西**：`getsockname`/`bind` 的 port-remap 与 inbound 的 listen/accept 路径本身；只改
  "什么时候/凭什么清理状态"。
