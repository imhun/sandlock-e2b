# 隔离边界：命名空间与过滤器的完整口径

> 2026-10-05 从 README「隔离边界：三种命名空间」小节沉淀出来 —— README 只留总览表与三条硬要求，
> 谁创建、为什么这么设计、代价是多少都在这里。逐项实测读数见 [benchmarks.md](benchmarks.md)。

沙箱不是一个容器、也不是一台 VM —— 它是**一个带 userns / pidns / netns 的进程**，
外面再套两层过滤器（Landlock 文件系统白名单 + seccomp 过滤器）。内核负责隔离，
平台只决定"谁在什么时候建哪个命名空间、谁有权写哪张映射"。

| 命名空间 | 开关 | 谁创建 | 买到什么 |
|---|---|---|---|
| **userns** | 形态自带（`E2B_PER_SANDBOX_UID`） | route B 槽位的子进程自己 `unshare(CLONE_NEWUSER)` | 身份翻译：**箱内 uid 0 ↔ 宿主侧沙箱池 uid** |
| **pidns** | `E2B_PID_NS`（部署清单全开，代码默认 `false`） | fork 的**中间进程**（先 userns，再 pidns） | 箱内看不见宿主与其他沙箱的 pid |
| **netns** | `E2B_ENABLE_NET_ISOLATION` + `E2B_FD_INJECT_CONNECT`（**必须成对**） | fork | 箱内只有 `lo`；出口由 supervisor 代连 |

## 1. userns：身份翻译，不是隔离

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
   `uid_map`/`gid_map`（[../envd_service/slot_identity.py](../envd_service/slot_identity.py)）；
3. 子进程轮询 `setresuid(X)` 直到成功，再 exec `sandlock-supervise`。

这也是为什么 route B 的槽位身份只能是 `agent-grant`：worker 自己既没有 `CAP_SETUID`，
也不该拥有"给一个进程安上任意身份"的能力。补充组在 `as_uid` 写 gid 映射时被
`setgroups=deny` 关掉，所以槽位进程保留的是 worker 的补充组（与旧路径一致）。

## 2. pidns：看不见别人，也看不见宿主

`E2B_PID_NS=1` 时沙箱是自己 PID 命名空间的 1 号进程：

- 宿主与其他沙箱的进程在箱内**不可见**；沙箱的 `ps` 只有自己；
- 判据是 `kill(<worker 的 pid>, 0)`：共享 pid ns 时它返回 **EPERM**（存在性 oracle ——
  能用来探到 worker 与别的沙箱活着），自有 pid ns 时返回 **ESRCH**；
- pid 1 会承担孤儿进程的 reaper 职责。

**实现约束**：非特权 `CLONE_NEWPID` 必须先有自己的 userns，所以 fork 会在**中间进程**里
先建 userns 再建 pid ns。这也是 2026-09-16 那个缺陷的位置：中间进程一开始只认"特权 remap"
和"自身身份"两种映射，route-B 箱在 pid_ns 下会掉回宿主槽位 uid（`id -u` = 21000），
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

## 4. 另外两层边界（不是命名空间）

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
