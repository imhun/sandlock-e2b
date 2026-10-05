# 分层归因：哪些 syscall 只靠外层 profile 挡住（2026-10-04）

配套 `findings-k0s-2026-10-04.md`。那一份给出静态判读；这一份给出**实测归因**，
因为"内层没挡"这句话必须能被测量，而不是被推理。

## 0. 为什么要做这件事

沙箱的 seccomp 是两层的 AND：

- **外层**：worker 容器的 profile（`deploy/seccomp/sandlock-worker.json`），
  `defaultAction: SCMP_ACT_ERRNO`，运行时解析为 **errno 38 (ENOSYS)**。
- **内层**：fork 自己的 BPF + seccomp-notify supervisor
  （`DEFAULT_BLOCKLIST_SYSCALLS` → EPERM；`chroot_path_syscalls()` → USER_NOTIF）。

**任何一层的缺失都不会立刻出事，但两层的强度不等价**：内层 blocklist 缺一个 syscall，
唯一的防线就变成"我们恰好挂了这份 profile"。换任何默认档运行时（裸 `docker run`、
多数 systemd 单元、自建 OCI 配置）这道防线就没了 —— STATIC-2 的升级预演已经实测过这一点
（`seccomp=unconfined` 下 7 个全部到达内核；**Docker/OCI 内置默认档放行 `fchmodat2`**）。

所以要回答的问题是：**这份清单上还有哪些名字，内层是空的。**

## 1. 方法：差分，而不是读表

单个 errno 无法归因，因为两层都回答 EPERM，内核也会为缺能力回答 EPERM。
但两层有一个**不对称**可以利用：

| 观测 | 结论 |
|---|---|
| profile ON → ENOSYS(38)，profile OFF → 别的 | **外层是唯一拒绝它的** |
| profile ON → EPERM(1)，profile OFF → EPERM(1) | 内层或内核能力，两者不可分（本清单不依赖这一格） |
| profile ON/OFF → 同一个 ENOSYS | 这个内核没有它，两层都无关（见 §5） |

于是把同一个探针跑两遍：

```
profile ON : deploy/seccomp/sandlock-worker.json
profile OFF: seccomp=unconfined
```

其余条件完全相同：同一个 `e2b-sandlock-test:latest` 镜像、同一套 cap、
`deploy/scripts/test-prod-shaped.sh` 的 phase-1 docker 参数（唯一新增是把 profile 标志
和一个标记变量转发进容器 —— 该脚本消费 `SECCOMP_PROFILE` 但**不转发**它，
所以探针无法自省跑在哪一份 profile 下，两次会写同一个文件、后者静默覆盖前者）。
沙箱用 `route_b_sandbox(IMAGE, rootfs)` 造，是**生产形态的 chroot/中介沙箱**，
不是合成的。扫 0..600 全部编号，每个在独立子进程里带 2 秒 alarm 调用（防挂死）。

**架构注意**：本地车道是 **linux/amd64**，生产是 **arm64**。
但本清单涉及的 15 个编号在 `syscalls` 0.8.1 的 `x86_64.rs` 与 `aarch64.rs` 中
**完全一致**（逐号核对），所以结论原样适用。

## 2. 实测结果：15 个只靠外层 profile

| nr | 名字（`syscalls` 0.8.1） | profile OFF 的回答 | 本轮 fork 修复是否覆盖 |
|---:|---|---|---|
| 451 | `cachestat` | EFAULT | **未覆盖** |
| 452 | `fchmodat2` | EFAULT | **已覆盖**（中介，非 blocklist） |
| 454 | `futex_wake` | EINVAL | 未覆盖 |
| 455 | `futex_wait` | EINVAL | 未覆盖 |
| 456 | `futex_requeue` | EINVAL | 未覆盖 |
| 459 | `lsm_get_self_attr` | EINVAL | **未覆盖** |
| 460 | `lsm_set_self_attr` | EINVAL | **未覆盖** |
| 461 | `lsm_list_modules` | EFAULT | **未覆盖** |
| 462 | `mseal` | **执行成功 (ret=0)** | **未覆盖** |
| 463 | `setxattrat` | EINVAL | **已覆盖**（blocklist） |
| 464 | `getxattrat` | EINVAL | **已覆盖**（blocklist） |
| 465 | `listxattrat` | EFAULT | **已覆盖**（blocklist） |
| 466 | `removexattrat` | EFAULT | **已覆盖**（blocklist） |
| 468 | `file_getattr` | EINVAL | **已覆盖**（blocklist） |
| 469 | `file_setattr` | EINVAL | **已覆盖**（blocklist） |

**已覆盖 7 / 仍敞开 8。**

## 3. 置信度：必须分清两档

这一节是这份文档里最重要的一段 —— **不要把两档当成一样的东西引用。**

### 3.1 三方互证，可直接引用（7 个）

`fchmodat2` + 4 个 xattr-at + `file_getattr`/`file_setattr`：

1. fork 自己的台账 `sys/path_surface.rs` 就是用这些编号登记的；
2. `syscalls` 0.8.1（**fork 自己解析编号用的就是它**）映射一致；
3. fork 的 `blocked_entries_are_actually_refused` 测试断言"台账标 Blocked 的项
   必须在解析后的 blocklist 里"，改了之后**通过**。

外加 `fchmodat2` 有一条行为证据：用真实路径 + 模式调用返回 **EPERM**
（内核的所有权检查，unprivileged），而用 statx 的参数形状调用返回 **EINVAL** ——
两个形状的回答都符合 fchmodat2。

### 3.2 名字未在运行时确认（8 个）—— 请按**编号**引用

`cachestat` / `futex_wake` / `futex_wait` / `futex_requeue` /
`lsm_get_self_attr` / `lsm_set_self_attr` / `lsm_list_modules` / `mseal`

这 8 个我做了行为确认但**没成功**，而且我找到了自己方法上的**根本缺陷**：

> 裸参扫描分不清"内核答了"和"**supervisor 答了**"。
> 对照实验：编号确认无疑的 `statx`(332) 被 fork **中介**（在 `chroot_path_syscalls()` 里），
> 用我的参数形状同样返回 EINVAL —— 那是 supervisor 的答复，不是内核的。
> 所以"内核没按预期应答"**不能**证明"名字错了"，只说明这次探测分辨不了层。

因此：**"15 个编号只靠外层"是实测事实；"其中 8 个对应这些名字"是台账映射，未在运行时确认。**

这不影响可执行性，但影响措辞：blocklist 里要写的是**名字**，而编号→名字的映射是
`syscalls` crate 做的 —— 也就是说，**只要 fork 用 crate 解析编号，
"把名字加进 blocklist" 与 "这些编号被内层拒绝" 就是同一件事**。
在一个 ENOSYS 的编号上加一条 blocklist 条目是无害的空操作。

## 4. fork 侧需要增强的清单（按建议动作分档）

### 档 A —— 建议 blocklist（无工作负载需求，纯粹是宿主状态面）

| 名字 | 为什么内层该挡 | 挡掉会不会伤到正常负载 |
|---|---|---|
| `cachestat` | 缓存旁路时序/占用侧信道：能对**任意可命名路径**问"这段在不在缓存里、大小多少"。共享文件系统上就是**跨租户侧信道**（与同节点另一沙箱的访问模式相关）。非特权，无需能力 | 不会。工作负载没有正当理由问缓存状态 |
| `lsm_get_self_attr` | LSM 属性读 | 不会 |
| `lsm_set_self_attr` | LSM 自属性写（`path`/`attr`/`size`）。非特权到 root，但可探测本机 LSM 状态 | 不会 |
| `lsm_list_modules` | **直接列出宿主已加载的 LSM 模块** —— 纯宿主指纹，零工作负载价值 | 不会 |
| `mseal` | 内层对它**毫无意见**，实测 profile 关掉后**执行成功**。6.10 引入的内存封存原语，只能封自己拥有的内存（所以不是逃逸），但它属于"新内存 syscall + 内层空白"这一类 | 基本不会。JIT/运行时用 `mprotect`，不用 `mseal`。若要保守，先量一遍 |

### 档 B —— **不建议**由内层挡

| 名字 | 判断 |
|---|---|
| `futex_wake` / `futex_wait` / `futex_requeue` | futex v2 家族。挡它有**挂死风险**：变体被 blocklist 掉而 glibc/运行时在用，表现是 futex 等待永不唤醒。今天它们被外层 profile 挡着（默认不在允许表），**这是 profile 的兼容性取舍，不是内层的洞**。若要收敛，应该先确认目标镜像里的 libc 用不用，再决定改 profile 还是改 blocklist |

### 档 C —— 本轮已处理（供对照）

`fchmodat2` 中介 + 6 个 blocklist，见 `remediation-SEC-R3-01.md` §8.3。
档 A 那 5 个（`cachestat` / `lsm_*` / `mseal`）已于 2026-10-04 第四轮加进
`DEFAULT_BLOCKLIST_SYSCALLS`，同时从 `NON_PATH_SYSCALLS` 移到
`UNMEDIATED_PATH_TAKING`（`Disposition::Blocked`）—— 原因见 §9.3。

### 档 D —— 更大的图景：两层都没意见的正常 syscall

同一次扫描里，"profile 关掉后仍然到达内核"的还有 **200 余个**正常 syscall
（`read`/`mmap`/`clone`/`socket`/…）。其中值得单独点名的：

- **`ioctl`（25）**：**两层都不管**，内层只拦 12 个 request code
  （`TIOCSTI`/`TIOCLINUX`/`SIOCGIF*`/`SIOCETHTOOL`），外层 profile 也不管。
  实际边界完全落在 **Landlock `IOCTL_DEV`** + 设备可达性上。这是设计选择（ioctl 无法按
  路径分类），但"seccomp 对 ioctl 几乎不设防"这句话应当被明确写下来，而不是留给读者推断。
- `memfd_create`：**故意放行**（运行时/JIT 需要，见 `structs.rs` 的注释）。
- `seccomp` / `landlock_create_ruleset` / `landlock_add_rule` / `landlock_restrict_self`：
  放行是安全的 —— 只能**收紧**，不能放宽（seccomp 过滤器只能 AND）。
- `perf_event_open`(298)：**实测内层确实挡住**（两次都 EPERM），确认 blocklist 生效。
- `ptrace` / `process_vm_readv` / `process_vm_writev` / `pidfd_getfd` /
  `io_uring_setup|enter|register` / `bpf` / `keyctl` / `add_key` / `request_key` /
  `userfaultfd` / `open_by_handle_at` / `name_to_handle_at` / `open_tree` /
  `statmount` / `mount` / `pivot_root` / `chroot` / `unshare` / `setns`：
  都在 `DEFAULT_BLOCKLIST_SYSCALLS` 里，**内层挡**（`perf_event_open` 已实测确认）。

## 5. 两条必须记下的"别再踩"

**① `uprobe` / `uretprobe`（x86_64 的 335/336）是个假线索。**
`syscalls` crate 把内核的**保留槽位**填成了这两个名字（**aarch64 表里根本没有**）。
实测两次配置都返回 **ENXIO(6)** —— 两层都没碰它，因为它在这个内核上根本不是 syscall。
如果照着 crate 的名字去写"旁路"，会得到一条不存在的发现。
真正值得记的是这条元规则：**`syscalls` crate 在保留编号上会填名，
所以"编号在某范围内"不等于"那个 syscall 存在"。**

**② ENOSYS 在两边都出现时，什么都不能推断。**
`set_thread_area`/`get_thread_area`/`epoll_ctl_old`/`epoll_wait_old`/`map_shadow_stack`
两次都是 ENOSYS —— 这个（7.0 OrbStack）内核没有它们，与两层无关。

## 6. 适用范围（重要，别过度解读）

- **生产（arm64，kernel `6.12.0-211.34.1.el10_2`）上这 15 个全部 ENOSYS**
  —— 这 7 个 syscall 是 6.13 才引入的。所以**今天线上没有可利用面**。
- 这份清单描述的是**"内核升到 ≥6.13 且外层 profile 不是这份"**那个窗口的暴露面。
- 触发条件是**两个都要满足**：内核实现了 + 外层换了。只满足一个都不构成问题
  （只满足前者 ⇒ 内层已挡住 7 个；只满足后者 ⇒ ENOSYS 挡住其余 8 个）。
- 唯一在**今天**就已经只靠外层的地方，是那 8 个里的 `lsm_*` / `cachestat` / `mseal`：
  它们是 6.5–6.10 引入的，在 6.12 上**存在**，而实测显示**外层 profile 挡着、内层不管**。
  这是本清单里唯一不需要等内核升级就成立的一条 —— 它是"profile 一换就敞开"。

## 7. 复现

```
sh tmp/audit3/run_layer_probe.sh
```

三段：① 带 profile 的全量扫描 → ② 不带 profile 的全量扫描 → ③ 对被点名编号的行为确认。
读数落在 `tmp/audit3/layer-probe-{worker-profile,unconfined}.json`。
架构对照表由 `tmp/audit3/extract_syscall_table.py` 从 `syscalls` 0.8.1 的 per-arch 源文件导出
（不是手抄常量）。

> 原始探针是临时文件，已从 `tests/security/` 移除（它们是探针不是门禁，
> 留在那里会被套件收集）。方法与读数是本文件的可复现依据。
## 9. 第四轮：`ioctl` 到底靠谁挡（2026-10-04）

前三轮都是"某个 syscall 被哪一层拒绝"。这一轮问的是另一个问题：
**`ioctl` 本身没有请求码粒度的 seccomp 保护时，剩下的边界在哪里** ——
答案是 Landlock，但它够不够要分开说。

### 9.1 三层实测

**Landlock ABI 8** ⇒ `LANDLOCK_ACCESS_FS_IOCTL_DEV`（ABI≥5）生效，
fork 的 `Protection::FsIoctlDev` 是 `Strict`（好：ABI<5 会直接拒绝建箱）。

| 节点 | 能答的请求码 |
|---|---|
| `/dev/ptmx` | 10 个：`FIONREAD`/`FIONBIO`/`FIONBAK`、`TCGETS`、`TCSETSW`、`TIOCGPGRP`、`TIOCGPTN`、`TIOCSPTLCK`、`TIOCSWINSZ`、`TIOCGWINSZ` |
| `/dev/null` `/dev/zero` `/dev/urandom` | 只有 `FIONBIO`（它们本来就没几个 ioctl） |
| `/dev/tty` | **打不开**（ENXIO，无控制终端） |
| `/dev/pts` | 只有自己的 `ptmx`（邻居数 0） |

授予集之外的 14 个节点全部 **EACCES**：`/dev/mem` `/dev/kmem` `/dev/kvm`
`/dev/sda` `/dev/kmsg` `/dev/net/tun` `/dev/dri/card0` `/dev/nvidiactl`
`/dev/input/event0` `/dev/console`，连 `/proc/self/mem` 也在内。

### 9.2 结论：**今天够用，但"够用的不是 Landlock"**

支撑今天安全的是三件事的叠加，Landlock 是其中最弱的一环：

1. **每沙箱独立 devpts 实例**（实测邻居 0）—— `TIOCSWINSZ`/`TCSETSW` 只能碰到自己建的 pty。
2. **BPF 那 12 个 code**（实测没漏，包括在 ptmx 这个"其它 10 个都答"的地方）。
3. **授予集只有 6 个良性节点**（实测）。

Landlock 只贡献了第 4 件事：集外节点被拒。

**三个结构性缺口**（不是今天的问题，是论证链上的洞）：

- **`IOCTL_DEV` 是路径粒度，ioctl 需要请求码粒度。** Landlock 没法说"在
  `/dev/ptmx` 上禁 `TIOCSTI` 但允许 `TIOCGPTN`"。唯一的请求码过滤器是
  fork 里硬编码的列表。
- **`IOCTL_DEV` 只管设备文件 —— 普通文件上的 ioctl 完全在射程之外。**
  实测普通文件上 `FIONREAD`/`FIONBIO` 答了；危险的
  `FIGETBSZ`/`FIGETFL`/`FS_IOC_FIEMAP` 在本沙箱文件系统上被拒，
  **但那是 VFS/文件系统拒的，不是 Landlock**。今天的保护是**偶然的**：
  在实现了 FIEMAP 的文件系统（XFS/ext4 都实现）上，能读文件的沙箱就能问
  "这个文件在磁盘哪里" —— 布局预言机。
- **`Protection::FsIoctlDev` 默认 `Strict`，但 `compute_fs_mask` 在保护被标成
  `Degradable` 时会静默丢掉 `IOCTL_DEV` 这一位，且不报错。** 默认 Strict 所以今天成立。

**前瞻风险**：GPU 节点在 fork 里按 **write mask** 授予 ⇒ 拿满 `IOCTL_DEV`，
而 NVIDIA 驱动的 ioctl 面很大。本机没 GPU 所以今天测不到。

### 9.3 第四轮实际改了什么（与原计划**相反**）

原计划是把 TTY 写入族（`TCSETS*` / `TIOCSWINSZ` / `TIOCSETD` / `TIOCSIG` …）
一并 deny。动手前查消费者，**推翻了原计划**：

- `envd_service/route_b.py:1264`、`envd_service/executors/local.py:93,135`
  用 `termios.TIOCSWINSZ`。
- `TCSETS*` 是 `openpty`/`tmux`/`vim`/`ssh`/`stty raw` 的依赖。

deny 掉 = 打断沙箱里的交互式程序，换来的安全性是**零**——威胁面不可达：

| 前提 | 实测 |
|---|---|
| `/dev/pts` 是 per-sandbox newinstance | 建箱后只有 `ptmx`；`openpty` 后只多出自己的 `0` |
| `/dev/tty` 打不开 | **ENXIO** |
| 沙箱内进程同 uid | 同沙箱内改别人终端本来就能 `kill()` 做到 |

**所以 TTY 写入族不是安全边界。** 修法改为把这三个前提写成不变式测试
（`tests/security/test_ioctl_inventory.py`），让缺口在前提失效时暴露，
而不是靠一个会打坏产品的 blocklist。

**deny 列表 12 → 20**（`seccomp_plan.rs`，12 + 补齐 1 + 新增 4 + 新增 3 = 20）：
- 补齐 `SIOCSIFFLAGS` —— **已定义但一直没在列表里**（`SIOCSIF*` 的 get 半族被挡、
  set 半族却敞着，是半截措施）。
- 新增 `SIOCSIFADDR` / `SIOCSIFBRDADDR` / `SIOCSIFNETMASK` / `SIOCSIFHWADDR`。
- 新增 `FS_IOC_FIEMAP` / `FIBMAP` / `FIGETBSZ`（普通文件布局预言机，见 §9.2）。

**blocklist +5**：`cachestat` / `lsm_get_self_attr` / `lsm_set_self_attr` /
`lsm_list_modules` / `mseal`。同时从 `NON_PATH_SYSCALLS` 移到
`UNMEDIATED_PATH_TAKING`（`Disposition::Blocked`）：那个表声称"不能命名
文件系统对象"，对 `cachestat` 与 `lsm_get_self_attr` 是**错的**（两者都接路径），
且与 `Blocked` 判定自相矛盾。

### 9.4 三个必须记录的测量教训

**① 我记忆里的 ioctl 常量错了 3/4 —— 而"猜错"在 seccomp 里不报错。**

| 名字 | 我猜的 | 实际 | 实测 |
|---|---|---|---|
| `FIBMAP` | `0x5401` | **`0x00000001`**（`_IO(0x00,1)`） | 猜的 ENOTTY |
| `FIGETBSZ` | `0x80045427` | **`0x00000002`**（`_IO(0x00,2)`） | 猜的 ENOTTY |
| `FIGETFL` | `0x80045426` | 不在 `linux/fs.h` | 猜的 ENOTTY |
| `FS_IOC_FIEMAP` | `0xC020660B` | `0xC020660B` ✓ | MATCH |

JEQ 猜错 ⇒ **静默不匹配** ⇒ 看起来像有控制其实没有。这比没有条目更糟。
已加变异测试：`FIGETBSZ` 改成错值后 `test_arg_filters_has_clone_ioctl_prctl_socket`
立刻红。另外 `libc` crate 在 `linux_like` 下**按架构分歧**（mips/sparc/arm 各异），
拿它做防漂移测试反而是个陷阱，所以最终用**权威 uapi 头 + 运行时实测**双源。

**② 只 deny `TCSETS` 会被完全绕过。**
实测 `TCGETS2`/`TCSETS2`/`TCSETSW2`/`TCSETSF2`（64 位 termios 代）
在 ptmx 上**全部 MATCH 且返回 ok**。deny base 族是自欺。

**③ 方向位分不开读写。**
整个 base 代 termios 族都是 `_IOC_NONE`，`TCGETS` 和 `TCSETS` 无法用方向位区分。

**两个副产物**：`socket()` 在沙箱内是 **EPERM**（所有 `SIOC*` 无处可下，
所以那 4 个是纵深防御，注释里写明不可运行时验证）；
**pty master 与 slave 答案不同**（`TIOCNOTTY` 在 master 上 ENOTTY、
在 slave 上 EPERM）—— 只测 master 会漏判，已写成
`test_pty_master_and_slave_disagree`。

### 9.5 门禁

- `tests/security/test_ioctl_inventory.py`（7 项，全绿）：把 §9.3 的三个前提钉住，
  并**反向钉住**"TTY 写入族**应该**可达"——哪天它不可达了，就该重新加回 deny 列表。
  已做变异验证：注入"共享 devpts + `/dev/tty` 可开" → 2 项变红。
- fork `cargo test -p sandlock-core --lib`：**915 passed / 2 failed**。
  那 2 个是环境项，已用 `git archive HEAD` pristine 副本复现同样 2 个失败
  （`procfs::tests::pid_ns_kill_host_pid_is_esrch`、
  `seccomp::notif::tests::dup_fd_from_pid_handles_worker_thread_fd`）。
- 顺带修了 `tests/unit/test_priv_maint_worker_gate.py` 的一个**真实测试缺陷**：
  它断言 `lchown` 必须 EPERM，但以 root 跑时 chown 会成功。改为断言
  "**不是 pool gate 拒绝的**"（两种结果都 exit 77，所以判别依据是消息不是退出码），
  root / nobody(65534) / uid-1000 三条 lane 均验证，并做两次变异确认它确实会红。

### 9.6 复现

```
docker run … e2b-sandlock-test:latest \
  pytest tests/security/test_ioctl_inventory.py -q
```

**可复现的门禁是仓库里的测试**，不是 `tmp/` 里的读数文件 ——
上面那条命令跑的 `tests/security/test_ioctl_inventory.py` 就是结论的固化形式。
一次性探针与原始读数（`tmp/audit3/` 下的 `ioctl-*.json`、`tty-sweep.json`）
留在 `tmp/`：它们被 `tests/unit/test_docs_only_point_at_repo_artifacts.py`
的 `tmp/` 引用规则**有意不覆盖**（该规则只匹配 `.py`/`.sh`，因为它要拦的是
"让读者去跑一个 tmp 脚本"这句话；`.json` 读数不是可执行入口）。

> 所以下面不再引用那些读数路径 —— 引用它们对读者没有价值：`tmp/` 会被清理，
> 新 checkout 里没有。§9.1–§9.4 的每个数字都能从上面那条命令得到，或从
> uapi 头文件直接核对。

> 方法（供重建）：① 枚举允许的设备面与请求码；② 校验常量 —— 用**权威 uapi 头**
> 而不是记忆，因为猜错在 seccomp 里是静默失效；③ 全量扫描 `_IOC` 编码空间，
> 取内核**实际实现**的集合，ptmx 与 pts slave 各一遍（两者答案不同）；
> ④ 读 Landlock ABI 确认 `IOCTL_DEV` 在位。
