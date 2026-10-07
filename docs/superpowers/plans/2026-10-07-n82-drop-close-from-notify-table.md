# N82 候选 ①：把 `close` 移出通知表（netlink cookie 改成"用时校验"）实施计划

**Goal:** 取消**每一个 `close` 都要 supervisor 往返**这件事 —— 生产形态的
`open+close` 从 **2 条通知/op** 降到 **1 条**，`close` 专项从"1 条/次"降到 **0**。

**为什么是它（N82 逐 op 读数的结论）**：`close` 进 `NETLINK_NOTIF_SYSCALLS`
（`seccomp_plan.rs:178`）只为一件与它无关的事 —— **注销 netlink cookie**。cBPF 看不了 fd
指向什么，所以按 fd 类型过滤不可能；于是沙箱里**每次** `close`（普通文件、管道、socket 全都算）
都要过一次通知。实测阶梯（本地车道 + `E2B_SANDBOX_NOTIFY_RATE_LIMIT=5000`，2026-10-07）：

| op | 读到的 op/s | 每 op 通知条数 |
|---|---|---|
| `uname` | 5086 | 1（= 5000 ÷ 1） |
| `openclose` | 2570 | 2（= 5000 ÷ 2） |
| `clone` | 1603 | —（**不是**通知受限：它吃满沙箱自己那 1 核，见 N86） |

**机制（读代码得到）**：`netlink::state::NetlinkState.cookies: Mutex<HashSet<(tgid, fd)>>`
记着 supervisor 注入的那些虚拟 netlink cookie fd；`getsockname`/`recvfrom`/`recvmsg`
的处理器先 `is_cookie(tgid, fd)` 判"这个 fd 是不是我们的 cookie"，而
`netlink::handlers::handle_close` 的全部工作就是"如果它是 cookie，就把它从集合里删掉"
（`dispatch.rs:854` 的注册注释自己写着：*Unregister on close so the (pid, fd) slot isn't
left in the cookie set once the child reuses the fd for something else*）。

**Architecture（一句话）：把"注销"从 close 时刻挪到"使用"时刻。**

- **删** `libc::SYS_close` from `NETLINK_NOTIF_SYSCALLS`，并删掉 `seccomp/dispatch.rs`
  里那处 `close` 注册（连带 `handlers::handle_close`）。
- **`is_cookie` 改成"带活体校验"**：注入 cookie 时记下该 socket 的 identity
  （`readlink("/proc/<tgid>/fd/<fd>")` 的 `socket:[<inode>]` 文本，或注入时 `fstat` 的
  `(st_dev, st_ino)`），命中集合后再比一次当前 fd 的 identity：
  - 相同 ⇒ 真 cookie，照旧虚拟化；
  - 不同 ⇒ fd **已被复用**，把这条 stale 条目删掉并 `Continue`（当作普通 fd）。
  这样"集合里有陈旧条目"永远不会产生错误答案，只多读一次 `/proc`（**只在 cookie 上**，
  而 cookie 是罕见的 netlink socket；close 是极热的）。
- **集合仍然要有界**：加一条"每 tgid 最多 N 条 + 插入时按 LRU 淘汰"的守卫（N82 原话
  "cookie 有界 + 覆盖"）。淘汰掉一个仍然活着的 cookie 的代价 = 那个 socket 之后不再被
  虚拟化（fail-open 到"看起来像真内核 netlink"），所以 N 取"比任何真实进程会同时开的
  netlink socket 数大一个量级"（例如 64），并**具名打点**；这是本次唯一要人拍的数字。

**Tech Stack:** Rust（`third_party/sandlock`：`seccomp_plan.rs`、`seccomp/dispatch.rs`、
`netlink/{state,handlers}.rs`）、`sandlock-dev:latest`（fork 门禁）、
`deploy/scripts/build-sandlock-wheels.sh`（wheel + `SHA256SUMS.supervise` 重钉）、
E2B 侧 `deploy/scripts/acceptance/probe_n82_traced_syscall_costs.py`、k0s（现场验收）。

**Spec:** `docs/open-issues.md` N82 的候选 ①；N86（clone 那半的口径）；本次两侧读数见 §7.51/§7.52。

## 判据（改动前后各一跑，同一车道、同一支探针）

1. **阶梯（限流开 = 5000，读的是"每 op 几条通知"）**：`--op openclose` 应从 **2570**
   抬到 ~**5086**（2 条 → 1 条）；`--op close`（每 op 1000 开 + 1000 关）应**翻倍**。
   这一档是本次的**主判据**：它只由"通知条数"决定，不掺额度。
2. **额度档（限流关，线上现在的形状）**：`--op openclose` 的 op 成本应从 ~107 µs
   （9343 op/s、0.832 核）降到 ~一半量级（少一次 supervisor 往返）。
3. **功能不回归**：`npm install` / `git status` 类真实负载（仓内没有现成探针 —— 用
   `probe_n82_traced_syscall_costs.py --op openclose` + 一条 `pip download` 冒烟替代），
   以及 netlink 虚拟化本身：`getsockname`/`recvfrom` 那几条既有契约用例
   （`crates/sandlock-core` 的 netlink 测试 + `tests/contract`）必须全绿。
4. **fd 复用钉子（本次新增，防的正是删掉 close 注销后的新风险）**：
   `open netlink socket → close → open 普通文件（拿到同一个 fd 号）→ getsockname/recvfrom`，
   期望**不**被虚拟化（`ENOTSOCK`/`EINVAL` 由内核答），且集合里的 stale 条目被清掉。

## 任务

- [x] **T1（RED）**：先写第 4 条那枚钉子（fd 复用），在**今天的树**上跑：它应该**绿**
  （今天靠 close 注销）；然后把 `close` 从表里删掉、**不动** `is_cookie`，同一枚钉子应变**红**
  —— 这就是"为什么必须有活体校验"的证据。
  **读数（2026-10-07）**：绿 → 删表成员后红（`{'closed': 'rc=0 errno=0', 'reused': True, 'virtualized': (324, 0), 'verdict': 'KERNEL:88'}` ——
  对**已关闭**的 fd 号仍然 `rc=0`，内核该答 `EBADF(9)`）→ 见 T2 后转绿。
- [x] **T2**：`is_cookie` 加活体校验（注入时记 identity，命中后复核）+ 集合上界（LRU + 具名打点）。
  T1 的钉子在"删了 close、加了校验"的树上必须**绿**。
  **落地形状与计划不同的一处**：上界**不需要 LRU** —— 键是 fd 号，而 fd 号来自沙箱自己的
  `RLIMIT_NOFILE`，所以 `insert` 每个进程最多那么多条；identity 记的是
  `readlink("/proc/<tgid>/fd/<fd>")`（`socket:[<inode>]`），命中后复核，不符/关掉就删条目并当普通 fd。
- [x] **T3**：删 `SYS_close` 与它的注册，跑 fork 门禁（`sandlock-dev:latest`），
  确认 netlink 那族用例全绿、无新增红。
  **读数**：`test_netlink_virt` **15/0**；整套门禁 `core_lib 941/0`（= 刷新后的基线）、
  `core_integ 575/0`、`ffi 104/0`；`cli` 相位红但是**既有问题**（`kernel_enforced_limits` 的
  CLI 参数定义，见 open-issues N87，与本次无关）。
- [ ] **T4**：`build-sandlock-wheels.sh` 重建 wheel + 重钉 `SHA256SUMS.supervise`，
  本地 compose 车道跑第 1/2 条读数（RED→GREEN 都留）。
- [ ] **T5**：发版（先控制面后 worker，同 §7.52 的顺序）+ 线上复跑第 1/2 条 + N82 探针全套
  （`--op {openclose,close,mmap,uname,chdir,getdents,stat,clone}`）。

## 风险与回退

- **最大的风险是 fd 复用**（T1 的钉子就是它的守卫）。若活体校验的成本在真实负载上意外显著，
  回退档是**保留 `close` 通知**但把 `handle_close` 降级为"只清集合、绝不做别的"——即今天的行为。
- **回退杆**：`NETLINK_NOTIF_SYSCALLS` 是一行常量；把 `libc::SYS_close` 加回去 + 恢复注册
  = 逐字节回到今天的表（wheel 重钉 → 镜像 → 滚 worker）。
- **不改的东西**：`socket`/`bind`/`getsockname`/`recvfrom`/`recvmsg` 五条**留在表里**
  （它们才是虚拟化的本体），本次只动 `close`。
