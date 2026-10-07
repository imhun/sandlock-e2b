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

- [ ] **T1**：写判据 1/2 两枚钉子，在**今天的树**上跑 —— 1 应绿（① 在位）、2 应红（readiness 现在
  只有 ① 的通知在兜底？先读一遍现状：若 2 已绿，说明还有别的兜底，先查清再动）。
- [ ] **T2**：readiness 改成用时校验（identity），T1 的第 2 枚钉子转绿。
- [ ] **T3**：inbound 按 **(a) 惰性替换 + 用时复核** 落地，T1 第 1 枚钉子按新契约改写并绿。
- [ ] **T4**：删掉 ① 的条件行，跑 fork 门禁（十相位；`cli` 相位的环境红按 N87 的口径单独记）。
- [ ] **T5**：重建 wheel + 本地车道两条读数（普通形状 + 带映射形状；后者需要一条 net_isolation 车道）
  → 发版 → 线上复跑 9/9 与 `--op openclose` 两档。

## 风险与回退

- **风险集中在 inbound 的语义变更**（(a) 让宿主端口占得比过去久）。回退杆：把 ① 那一行
  `if features.inbound_port_map { nrs.push(SYS_close) }` 加回 `seccomp_plan`（= 回到 N88 ① 的形状），
  或整条 `close` 加回 `NETLINK_NOTIF_SYSCALLS`（③，最保守）。
- **TOCTOU 说明**：readiness 的用时校验与 netlink 同形 —— 校验与 supervisor 的合成答复之间，兄弟线程
  可以关掉并复用 fd；那条竞态是应用自身的竞态（POSIX 下"关掉 fd 号的同时使用它"本来就没有定义），
  netlink 那一版已按同样口径记录，这里沿用同一句话，不再新开一轮论证。
- **不改的东西**：`getsockname`/`bind` 的 port-remap 与 inbound 的 listen/accept 路径本身；只改
  "什么时候/凭什么清理状态"。
