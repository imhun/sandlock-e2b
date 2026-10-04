# 安全框架

写给下一个人接手这块时看的。不是审计报告 —— 报告在 `findings*.md`，这里只回答两个问题：
**防线摆在哪、哪条缝要往哪查**。

所有读数除注明外都是 2026-10-04 在 k0s 集群（2 节点 arm64、kernel `6.12.0-211.34.1.el10_2`、
containerd 2.3.4、Rocky 10.2、部署版本 `0.1.0-979`）实测的。

## 威胁模型

攻击者已经**在沙箱内拿到任意代码执行**：任意命令、PTY、文件 API、任意 env、任意 cwd、
网络按策略。读得着自己的整个 rootfs，可以无限次试。

按后果分四类，和 `attack-surface.md` 的编号一致：

| | 目标 | 典型后果 |
|---|---|---|
| L1 | 沙箱 → 宿主 / worker | 逃逸，读到节点上别的租户 |
| L2 | 沙箱 → 另一个沙箱 | 横向，读别人的 workspace |
| L3 | 沙箱 → 平台面 | 越权，拿到控制面或 c3-agent |
| L4 | 打垮 worker / 节点 | 可用性 |

不在模型内：控制面 API key 泄露、节点 root 失陷、DNS 劫持。这些是运维面的事。

## 架构图

```
                          ┌───────────────────────────────────┐
   客户端 ──── HTTPS ────▶ │ 控制面 gateway :3000              │
   (API key)              │  POST /sandboxes  · 准入/配额/记账 │
                          │  X-Access-Token → runtime registry│
                          └───────┬───────────────────┬───────┘
                                  │ 内部 API（零对外暴露）│
                    ┌─────────────▼──────┐      ┌─────▼──────────────┐
                    │ e2b-worker (pod)   │      │ e2b-c3-agent       │
                    │  envd（执行代理）  │      │  hostPID: true     │
                    │                    │      │  无 seccompProfile │
                    │ ┌────────────────┐ │      │  ← NetworkPolicy   │
                    │ │ 沙箱进程        │ │      │    把 worker pod IP │
                    │ │ uid 池内独占    │ │      │    全量 blackhole  │
                    │ │ PID ns / net ns│ │      └────────────────────┘
                    │ └───────┬────────┘ │
                    └─────────┼──────────┘
                              │ ① seccomp 内层（fork，进程级）
                              │ ② Landlock ABI 6（路径 + IOCTL_DEV）
                              │ ③ chroot 中介 / procfs 合成 / netlink 虚拟
                              ▼
                    ┌──────────────────────────┐
                    │  worker 容器（hostPID）  │
                    │  ┌────────────────────┐  │
                    │  │ supervisor（supervise）│ │  ← 代执行 send/connect
                    │  │ 持 seccomp notify fd │ │     chroot 路径翻译
                    │  └────────────────────┘  │
                    │  共享 NAS: workspaces/    │
                    │           state/_images   │
                    └──────────────────────────┘
                              │
                              │ ④ 外层 seccomp profile
                              │   （deploy/seccomp/sandlock-worker.json）
                              ▼
                    ┌──────────────────────────┐
                    │  宿主机 kernel            │
                    │  ★ 沙箱够不到的那一层 ★  │
                    └──────────────────────────┘
```

四层里前三层是沙箱代码（fork + envd），第四层是部署配置。
**打穿前三层只能到 worker 容器，打穿第四层才到宿主** —— 第四层是唯一的宿主边界。

## 各层职责

### ① seccomp 内层（`third_party/sandlock`）

进程级、默认动作拒绝。两类规则：

- **blocklist**（`sys/structs.rs::DEFAULT_BLOCKLIST_SYSCALLS`）：按**名字**。
  当前含 mount / pivot_root / chroot / keyctl / add_key / request_key /
  perf_event_open / bpf / userfaultfd / cachestat / lsm_\* / mseal /
  四个 xattr-at / statmount 等。名字走 `syscalls` crate 解析，"加进表" 与
  "这个编号被拒" 是同一件事。
- **arg 过滤器**（`seccomp_plan.rs`）：按**参数**。三处
  - `clone`：`CLONE_NEW*` 位
  - `socket`：`SOCK_RAW`/`SOCK_DGRAM` on `AF_INET`/`AF_INET6`（无网络规则时）
  - `ioctl`：20 个请求码

  ioctl 那份是重点。`ioctl` 在 seccomp 里**没有请求码粒度**，JEQ 链是唯一有粒度的地方，
  所以它列了什么就是全部。20 条分三类：
  - 已拒：`TIOCSTI` / `TIOCLINUX`（终端注入）、`SIOCGIF*` + `SIOCSIF*` + `SIOCETHTOOL`
  - 新拒：`FS_IOC_FIEMAP` / `FIBMAP` / `FIGETBSZ`（普通文件布局预言机 —— Landlock 的
    `FS_IOCTL_DEV` 只管设备文件，普通文件上的 ioctl 完全无闸）
  - **故意不拒**：`TCSETS*` / `TIOCSWINSZ` / `TIOCSETD` 等终端写入族。理由见下面「冗余」

**arg 过滤器的失效是静默的**：请求码写错 ⇒ JEQ 永不匹配 ⇒ 表里看着有、实际不拒。
所以 `context/tests.rs` 把这三个常量值连同它们在过滤器中的存在一起钉住。

### ② Landlock（fork `landlock.rs` / `protection.rs`）

路径粒度 + 设备 ioctl 粒度。默认 `strict_all()`：够不着的保护直接让 `build()`
**拒绝建箱**，不静默降级；只有显式标 `Degradable` 的才会静默丢 mask 位。

当前六项及其 floor：`FsRefer` 2、`FsTruncate` 3、`NetTcp` 4、`FsIoctlDev` 5、
`SignalScope` 6、`AbstractUnixSocketScope` 6。**最高 6 = 生产 ABI**，所以线上无降级。
（本地车道 ABI 8，但这不是问题 —— 问题是"声明的 floor 够不够得着"，见
`protection.rs` 里那两个 `*_deployed_abi` 测试。）

`IOCTL_DEV` 从 ABI 5 起才有，6 满足 ⇒ 设备节点那层闸在线上是生效的。

### ③ 中介层（envd + fork supervisor）

seccomp 与 Landlock 抓不到的语义在这里：

- **路径翻译**：`chroot` 形态下子进程的**内核根是宿主 `/`**，fork 不调 `chroot(2)`，
  而是让子进程 chdir 进 rootfs 内的宿主路径，再把每个被拦的路径 syscall 翻成
  `<chroot_root>/<虚拟路径>`。推论：**任何不在 `chroot_path_syscalls()` 里的带路径
  syscall 都会以宿主根为基准执行** —— `chroot` 自己就是这样漏过一次。
- **procfs 合成**（`procfs.rs`）：`/proc/net/dev`、`if_inet6`、`tcp`/`tcp6`
  （按已绑定端口过滤）都是生成的不是真的；`/proc/kcore`、`/sys` 直接拒。
- **netlink 虚拟**：只有 `NETLINK_ROUTE` 放行，换成 `socketpair(AF_UNIX)` 由
  supervisor 应答合成报文；别的协议（`NETLINK_XFRM`/`NETLINK_KEY`）EAFNOSUPPORT。
  socket family 白名单只有四个：`AF_UNIX`/`AF_INET`/`AF_INET6`/`AF_NETLINK`。
- **代执行**：`connect`/`sendto` 由 supervisor 在 **worker netns** 里代建连。
  ⇒ **per-sandbox netns 只隔离入站与回环，出站目的地址完全由规则决定**，netns
  不构成出站防线。

### ④ 外层 profile（`deploy/seccomp/sandlock-worker.json`）

`SCMP_ACT_ERRNO` 兜底 + 416 条允许项。默认动作给 **ENOSYS(38)**，
这和内层 blocklist 的 **EPERM** 是两种指纹，调试时别混。

**已知只靠它挡的**：15 个 6.13 才引入的 syscall（`fchmodat2`、四个 xattr-at、
`file_getattr`/`file_setattr` 等）。生产 kernel 6.12 上它们全 ENOSYS，所以今天没有
可利用面；但这份清单描述的是"内核升到 ≥6.13 且外层换了"那个窗口。逐项见
`layer-attribution-2026-10-04.md`。

### ⑤ c3-agent

`hostPID: true`、**无 seccompProfile**、非 root。唯一的隔离手段是那条 NetworkPolicy：
把 worker pod IP 全量 blackhole，所以沙箱够不到它，且沙箱不知道内部 token。
这是当前架构里**最薄的一环** —— 一个网络策略变更就能打通。

## 冗余原则：两道腿

同一个属性常常由两个独立机制保，改一个另一个还在。两个已知例子：

**① 隐藏宿主网卡名** —— netlink 合成 + `SIOCGIF*` ioctl deny list。
危险在于两者互不暗示：有人删掉 ioctl 那份（"netlink 已经虚拟化了，ioctl 那份多余"），
`SIOCGIFCONF` 会立刻重新泄露 `tunl0`/`eth0`。钉在
`tests/security/test_socket_families.py::test_netlink_route_discloses_no_host_interface`。

**② 终端写入族不加 deny 的两个前提** —— `/dev/pts` 是每沙箱 devpts 实例，
`/dev/tty` 打不开（ENXIO），加上沙箱内同 uid。加了 deny 会打断 `tmux`/`vim`/`ssh`/
`openpty` 和平台自己的 PTY endpoint（`envd_service/route_b.py:1266`、
`envd_service/executors/local.py:93,135`），
而它能攻击的面不可达。前提变了就该加回去 —— 钉在
`tests/security/test_ioctl_inventory.py`。**两个前提缺一，测试变红。**

## 已知结构缺口

按"能不能被沙箱够到"分，不是按严重度。

**够不到，但根子在外面**

| 项 | 状态 |
|---|---|
| CVE-2026-53362（IPv6 fraggap，KEV 已确认在野利用） | kernel `6.12.0-211` 未打（修复在 6.12.95）。**输入原语实测可达**：带网络规则的沙箱能建 UDPv6 socket、`MSG_MORE\|MSG_SPLICE_PAGES` 能进内核、超长包能进 paged 分片路径。公开 exploit（`Jevil36239/ipv6_frag_escape`）是 x86_64-only 且要 LA57 五级页表，跑不起来 —— 但那是利用链的缺失，不是漏洞的缺失。**只有打补丁能挡** |
| 6.13 `*at` 族 | 15 个只靠外层 profile。见上 |

**够得到，已挡，但机制是巧合而非策略**

| 项 | 现状 |
|---|---|
| `AF_RXRPC`（CVE-2026-43500 的面） | 白名单挡住，但节点上模块恰好不可用。现在两层都在：白名单 + `REFUSED_FAMILIES` 具名 |
| `AF_KEY` | 同上，`REFUSED_FAMILIES` 已具名 |

**架构级的已知项**（在 `findings*.md` / `open-issues.md`，此处不重复）

- 平台面无租户隔离（OBS-6）：一个 API key 能管所有沙箱
- 共享 workspace 形态下 `max_disk` 不生效（OBS-5）
- `c3-agent` 无 syscall 过滤，靠网络策略单点

## 门禁在哪

改了哪层，就该跑哪组。

| 改什么 | 跑什么 |
|---|---|
| fork 的 syscall 面 | `cargo test -p sandlock-core --lib`（918+ 绿，2 个环境项） |
| fork 的 socket/netlink 面 | 同上 + `tests/security/test_socket_families.py`（需沙箱车队） |
| Landlock 保护或 floor | 同上 + `protection.rs` 的 `*_deployed_abi` 测试 |
| envd 鉴权 | `tests/security/test_envd_token_fail_closed.py` |
| c3 特权面 | `tests/unit/test_priv_maint_worker_gate.py` |
| 宿主接口名 | `tests/security/test_ioctl_inventory.py` |

`tests/security/` 里需要真实沙箱车队，**没跑就是没跑**，不要拿别的套件绿了充数。

## 改这份文档的规矩

1. 结论要么有实测读数，要么有代码位置。两者都没有就是猜测，标出来。
2. 「没找到证据」和「确认安全」是两回事，本文分开写。
3. 归因要跨层对照。同一探测在沙箱内和沙箱外各跑一遍，否则分不清是谁拒的 ——
   单跑沙箱会把"模块恰好没加载"读成"沙箱挡住了"。