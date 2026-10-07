# 隔离边界：命名空间与过滤器的完整口径

> 2026-10-05 从 README「隔离边界：三种命名空间」小节沉淀出来 —— README 只留总览表与三条硬要求，
> 谁创建、为什么这么设计、代价是多少都在这里。逐项实测读数见 [benchmarks.md](benchmarks.md)。

沙箱不是一个容器、也不是一台 VM —— 它是**一个带 userns / pidns / netns 的进程**，
外面再套两层过滤器（Landlock 文件系统白名单 + seccomp 过滤器）。内核负责隔离，
平台只决定"谁在什么时候建哪个命名空间、谁有权写哪张映射"。

| 命名空间 | 开关 | 谁创建 | 买到什么 |
|---|---|---|---|
| **userns** | 形态自带（`E2B_PER_SANDBOX_UID`） | own identity 槽位的子进程自己 `unshare(CLONE_NEWUSER)` | 身份翻译：**箱内 uid 0 ↔ 宿主侧沙箱池 uid** |
| **pidns** | `E2B_PID_NS`（部署清单全开，代码默认 `false`） | 建箱的 `clone3` 一次带 `NEWUSER\|NEWPID`（N80 之前是 fork 的中间进程） | 箱内看不见宿主与其他沙箱的 pid |
| **netns** | `E2B_ENABLE_NET_ISOLATION` + `E2B_FD_INJECT_CONNECT`（**必须成对**） | fork | 箱内只有 `lo`；出口由 supervisor 代连 |

## 1. userns：身份翻译，不是隔离

> **旧称 route B（2026-10 按本质改名）**：这套机制现在的名字是 **own identity**（选型：这份工作以谁的 uid 在
> 宿主上发生），承载它的进程池叫 **slot**，身份怎么授予叫 **identity grant**。历史文档与发版记录保留旧名。

每个沙箱一个用户命名空间，映射**只有一条**：箱内 `uid 0` ↔ 宿主侧**这个沙箱自己的池 uid**
（fork 的 F18 自映射）。于是：

- 箱内 `id -u` = `0`（官方 SDK 与用户代码都期望的"沙箱内是 root"）；
- 它在宿主上写出的每个文件都属于那个池 uid，**不是** worker、更不是 root；
- worker（uid 65534）只是那棵树的**属组**（`0770`），这就是它的数据面权限。

**谁写映射是这套设计的核心。** 非 root 进程不允许把任意宿主 uid 映射进命名空间
（内核只让映射"自己"），所以：

1. worker fork 一个子进程，子进程自己 `unshare(CLONE_NEWUSER)` 并向控制面报
   `{sandbox_id, pid}` —— 它**不知道也不需要知道** uid；
2. 控制面按自己记录里的 `sandbox → host_uid` 查出 uid，指令**本节点 agent** 写
   `uid_map`/`gid_map`（[../envd_service/identity_grant.py](../envd_service/identity_grant.py)）；
3. 子进程轮询 `setresuid(X)` 直到成功，再 exec `sandlock-supervise`。

这也是为什么 own identity 的槽位身份只能是 `agent-grant`：worker 自己既没有 `CAP_SETUID`，
也不该拥有"给一个进程安上任意身份"的能力。补充组在 `as_uid` 写 gid 映射时被
`setgroups=deny` 关掉，所以槽位进程保留的是 worker 的补充组（与旧路径一致）。

## 2. pidns：看不见别人，也看不见宿主

`E2B_PID_NS=1` 时沙箱是自己 PID 命名空间的 1 号进程：

- 宿主与其他沙箱的进程在箱内**不可见**；沙箱的 `ps` 只有自己；
- 判据是 `kill(<worker 的 pid>, 0)`：共享 pid ns 时它返回 **EPERM**（存在性 oracle ——
  能用来探到 worker 与别的沙箱活着），自有 pid ns 时返回 **ESRCH**；
- pid 1 会承担孤儿进程的 reaper 职责。

**实现约束**：非特权 `CLONE_NEWPID` 必须先有自己的 userns，所以建箱时一次
`clone3(CLONE_NEWUSER|CLONE_NEWPID|…)` 同时给出两者（N80 之前由中间进程先 unshare userns、
再 unshare pidns；两个进程角色的分工与现场读数见 [§4](#4-进程结构一个二进制两个进程角色)）。
这也是 2026-09-16 那个缺陷的位置：当时的中间进程只认"特权 remap"
和"自身身份"两种映射，own-identity 箱在 pid_ns 下会掉回宿主槽位 uid（`id -u` = 21000），
修法是让它按与 `confine_child` 同一套三选一挑映射（fork `5b16855`）。

**代价**：pid_ns 打开后 fork 要拦 stat 族（`newfstatat`/`statx`/`faccessat`/`readlinkat`…）。
部署形态（模板 rootfs + chroot 中介）里这些调用**本来就已经过 supervisor**，实测增量
≤ 2 µs/次；只有"没有任何路径中介"的裸形态才看得见真实单价（+80~90 µs/次）。
口径与实测表：[production-deployment-requirements.md §2.4.10](production-deployment-requirements.md)。

## 3. netns：只留 loopback，出口由 supervisor 代做

`E2B_ENABLE_NET_ISOLATION=1` + `E2B_FD_INJECT_CONNECT=1`（**两个必须一起给**）时，
每个沙箱在自己的网络命名空间里起来，里面只有 `lo`：

- **出站**：沙箱的 `connect()` 被 supervisor 接住，由 supervisor 在宿主侧建连，再把
  **已连接的 fd 注入**到沙箱自己的 socket fd 上 —— 被接住的 `connect()` 返回 0，
  CPython 的 socket 语义不变（不是靠 on-behalf 重写）。`allowOut`/`denyOut`/`rules`/
  `egressProxy`/SSRF 护栏都在这一步判定。
- **成对是硬要求**：只开 `E2B_ENABLE_NET_ISOLATION` 会让沙箱变成 loopback-only，
  故障表现为"网络超时"而不是报错，所以 `create_app` 启动期直接拒绝
  （`NET_ISOLATION_PAIRING_ERROR`）；确实想要不能出网的沙箱要显式声明
  `E2B_NET_ISOLATION_ALLOW_LOOPBACK_ONLY=1`。
- **入站**：netns 里外部拨不进来，需要端口映射（`net_bind_inject`：把沙箱的 `bind()`
  换成 supervisor 在宿主 loopback 上建的 socket），端口带 `61000–65535`（4536 个，
  内部 MCP 网关用它，远大于单节点沙箱上限）。
- **DNS / 通配域名**：每沙箱的 loopback DNS 网关（`127.0.1.x:53`）把通配子域解析成合成 IP，
  再由 supervisor 代连并做二次校验。因为它 bind 在**沙箱自己的 netns** 里
  （root-in-userns 自带 `CAP_NET_BIND_SERVICE`），清单里不再需要
  `ip_unprivileged_port_start=0` 那种"低端口窗口"（k8s 2026-09-17 / compose 2026-09-16 撤掉）。
- **代价**：短连接建立 p50 0.034 → 0.291 ms（约 8.5×，长连接/连接池无感）；
  非阻塞 `connect_ex()` 直接返回 0（共享 netns 下是 `EINPROGRESS`）。
  逐项实测见 [production-deployment-requirements.md §2.4.6](production-deployment-requirements.md)。
- `E2B_ENABLE_NETNS` 是更早的 veth-pool 形态遗留旋钮，**默认关且对现在的形态不生效**；
  worker 启动时那套 veth 池 NAT（[../envd_service/netns.py](../envd_service/netns.py)）只在旧形态下才需要。

## 4. 进程结构：一个二进制，两个进程角色

沙箱活着的时候宿主侧是**两个进程**（N80 之前是三个），`exe` 全是同一个
`bin/sandlock-supervise`，全程 fork/clone 不 exec —— `ps` 里只有一个 `sandlock-superv`：

```text
worker 里的 envd 进程
└─ A sandlock-superv     spawn_child：clone3(NEWUSER|NEWPID|NEWNS|NEWNET) → setresuid(X) → exec
   └─ C sandlock-init    新 userns/pidns 里的 PID 1（宿主侧的直接父就是 A）
      └─ 主工作负载 / 之后每条 exec
```

| 角色 | comm | 线程 / wchan | 职责 |
|---|---|---|---|
| **A 真 supervisor** | `sandlock-superv` | 6 线程：主 + `sandlock-events` + 5×`tokio-rt-worker`；`unix_stream_data_wait` | 握 worker 的 control fd **与 seccomp listener fd**，应答每一次被拦的 syscall；用 `clone3` 建 leader 并直接 `wait` 它 |
| **C 沙箱 PID 1** | `sandlock-init` | 1 线程；`poll_schedule_timeout` | 沙箱内控制循环：起负载、收孤儿、按组发信号 |

**A 是唯一应答 seccomp 通知的那个**：listener fd 由子进程建好、把 fd 号走专用管道报回父进程
（`crates/sandlock-core/src/sandbox.rs:2874`），接收循环 `recv_notif → handle_notification(...).await`
（`seccomp/notif.rs:2777` / `:2812`）跑在 A 自己的 tokio runtime 上 —— **限流"睡满该秒窗口"那段就在
这个循环里**（`notif.rs:2748`，[N79](open-issues.md)）。2026-10-05 现场实测：沙箱里 hammer 3×6000 次
stat，**只有 A 烧 CPU（合计 0.48 s ≈ 26.7 µs/次，与探针 p50 26 µs 一致），B、C 全程 0 tick**。

**中间进程为什么没有了（N80，2026-10-06）**：`unshare(CLONE_NEWUSER)` 拒绝多线程调用者，而 A
握着一个 tokio runtime —— 这就是过去必须 fork 出一个单线程中间进程、由它去
*unshare user ns → 写 map → unshare NEWPID → fork* 的全部原因。`clone3` 把新命名空间给**子进程**，
与调用者的线程数无关，所以 A 直接 `clone3(CLONE_NEWUSER|CLONE_NEWPID|CLONE_NEWNS|CLONE_NEWNET)`
把 leader 生进四个 ns。连带消失的是中间进程留下的两样东西：leader 宿主 pid 的专用回传管道
（现在就是 `clone3` 的返回值）和退出码转发（现在 A 直接 `wait` C）。
`unshare(CLONE_NEWPID)` "不移动调用者、必须再 fork"的语义也从这条路上消失了。

**C 是怎么被认出来的**：fork 后不 exec，靠 `prctl(PR_SET_NAME)` 改成 `sandlock-init`
（`context.rs:414`，注释原话：*"the in-process PID-1, which has no `execve` to set its name from
argv[0]"*）。职责（`init/mod.rs` 模块注释）：

- 读 `CONTROL_FD` 上的 `Req`：`RunMain` fork+exec 主负载、`RunExec` 执行后续每条命令；
  **每个子进程继承它的 seccomp filter 与 Landlock ruleset**（*"so they share the one supervisor"*）；
- 每个子进程 `setpgid(0,0)` 自成进程组 ⇒ guest 的 `killpg` 只打得到自己那棵子树，不连坐容器（SECE-6）；
- 自称 child subreaper，每轮 `waitpid(-1, WNOHANG)` 收养并收割所有孤儿（SL-6）；
- 实例级信号 = 先 `killpg` + 对跑出自己组/会的逃逸者补 `pidfd_send_signal`（FUP-10）；
  **线上没有"按 pid 发信号"的动词**，控制通道被攻破也只能要求实例级投递；
- 主负载退出 ⇒ 给每个注册组发信号、自己退出 ⇒ 容器结束。

**箱内看不到这两个进程**：`/proc` 在真根下是个**空目录**（内核 procfs 挂不上，三形态实测 EPERM），
但只要 open/stat `/proc/*`，拦下来的进程直接回 **EACCES**（内核对空目录本该回 ENOENT）—— stat 族
那个 EACCES 出自 `crates/sandlock-core/src/procfs.rs:920 handle_proc_stat_family`，**正是
[N79](open-issues.md) 要动的那条**；顺带用 shell 连做 6000 次 `[ -e ]`（每次一个 stat）实测
0.99 / 1.04 / 1.04 s（5000 次预算 ≈133 ms 忙 + ~870 ms 限流睡），把 N79 的算式现场复现了一遍。

## 5. 另外两层边界（不是命名空间）

- **Landlock**：文件系统访问白名单（`fs_writable` / `fs_mount` 落到策略里），
  这是"沙箱只能碰自己的树"的第一道；
- **seccomp**：worker 与沙箱各自一份过滤器。worker 必须真的跑在我们发的 profile 下
  （`E2B_REQUIRE_SECCOMP_FILTER` 会在启动时拒绝"没有过滤器"的容器），
  因为沙箱是从 worker 继承 syscall 面的；
- 另外，开启 `E2B_REAL_ROOT`（N35）时 fork 还会建一个沙箱自己的 **mount namespace**，
  在 `pivot_root` 进去之后**丢掉 `CAP_SYS_ADMIN`**，让内核自己去解析 `#!` 脚本与静态二进制
  （[chroot-workspace-exec.md](chroot-workspace-exec.md)）。

一句话：**userns 管"我是谁"、pidns 管"我看得见谁"、netns 管"我能连谁"，
Landlock/seccomp 管"我能碰什么、我能调什么"。** 四者叠加才是这个沙箱的边界；
它们都由同一份部署清单显式声明，且每一项都有回退开关。
