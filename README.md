# sandlock-e2b

**E2B 兼容的沙箱服务端**：官方 `e2b` Python / JS SDK **零代码修改**即可接入，运行时用
[Sandlock](https://github.com/imhun/sandlock)（Landlock + seccomp-bpf + seccomp user
notification）在自己机器的 Linux 内核上跑用户代码 —— 不需要 Firecracker，也不需要虚拟化。

## 核心优势

> **一句话**：把 E2B 的开发者体验搬到你自己机房的普通 Linux 服务器上 ——
> **比容器安全，比 microVM 轻，且不锁定任何云厂商。**

- **无需虚拟化，比 microVM 更轻。** 不用 Firecracker 一类 microVM，也不需要 KVM、嵌套虚拟化或 GPU：
  沙箱是内核原语（用户 / PID / 网络命名空间 + Landlock + seccomp）**纵深加固**的进程。没有 guest
  内核、没有固定的虚拟化内存开销，一台普通 Linux 服务器就能跑 —— 实测**镜像预缓存后建箱 p50 0.66 s、
  每个活动沙箱额外内存 ≈16 MiB、命令往返 p50 0.10 s（内网侧 0.033 s）**，却不像共享内核的容器那样
  只有一层薄边界（[见下方实测](#轻量化到什么程度实测)）。
- **换三个环境变量就能迁过来。** 官方 `e2b` Python / JS SDK **零代码修改**：沙箱、命令与 PTY、
  文件、卷、快照与 fork、暂停/恢复、网络策略、模板本地构建、MCP 网关全部兼容。现有 E2B 应用
  把地址指过来即可，代码与 SDK 都不用动。
- **默认最小权限，纵深防御。** 沙箱内是 root，落到宿主机上只是它**自己的隔离身份**，永远不是宿主
  root；每个沙箱独立的 PID 与网络命名空间，看不见宿主、也看不见别的租户；文件访问由内核级白名单
  约束；出站流量必须过策略（白/黑名单、域名 ACL、企业代理、SSRF 护栏），默认拒绝直连内网。
- **平台自己也不能提权。** 执行节点以非特权身份运行、**不带任何特权二进制**；所有需要特权的动作
  收拢到每节点一个受控组件，它只按平台的记录执行，**不接受调用方指定的路径或身份**；执行节点与
  它之间没有任何通道 —— 攻下一个沙箱、甚至拿到执行节点的进程身份，都换不到别的租户。
- **生产可用，不是 demo。** 多节点调度、资源与磁盘配额、暂停/恢复与检查点、快照与 fork、自愈、
  自动扩缩容、指标与生命周期日志全部在仓库内闭环；纯自托管、可离线、可跑在现有 K8s 集群上，
  没有"企业版才解锁"的开关。
- **失败可见，不做假成功。** 未实现的 API 明确报错而不是假装成功；危险的半配置在启动时就被拒绝
  并打印原因；测不到的数值如实报 `unknown` 而不是 `0` —— 安全与配额上不会给你一个"看起来正常"的绿色。

### 轻量化到什么程度（实测）

下面这组读数是 2026-10-01 在出厂集群（`0.1.0-824`，2 节点 arm64）上用
[deploy/scripts/acceptance/lightweight_metrics_probe.py](deploy/scripts/acceptance/lightweight_metrics_probe.py)
现跑的 —— 一条命令就能复跑，每个数字都附了口径：

**① 镜像预缓存后，建箱很快**

- **建箱 p50 = 0.66 s、p95 = 0.73 s**（n=10；从开发机经入口实测，含 SDK 请求 → 控制面调度 →
  worker 建工作目录 → 销毁。每轮建完立刻 kill，不占容量）。
- 镜像没进缓存时也不贵：一次解包（2111 个文件的 python-slim rootfs）在**节点本地 0.26 s**
  —— 这也是方案刻意做的事：OCI tar 放共享卷、rootfs 解到节点本地缓存。冷节点用预热端点
  跑一次 **18.8 s**，之后一直命中。
- 旁证：4 个沙箱跨 2 节点的端到端冒烟（建箱 + 命令 + 文件 + stdin）整轮 **8.7 s**；
  加上模板构建 → registry → worker 拉取 → 解 rootfs → MCP 的完整冒烟 **20.6 s**。
- **这 0.66 s 花在哪**（2026-10-01 逐段实测）：客户端到入口的网络 **~40 ms**、控制面认证 +
  记录查询（幂等建箱对照）**2 ms**、控制面→worker 一跳 **7 ms**、worker 建树 + 把树交给沙箱 uid +
  写记录 **~120 ms**，而**约 0.45–0.55 s 是去镜像仓库解析基础镜像** —— 建箱时每个 worker 各自
  发约 6 次 HTTPS（`dockerauth…/auth` + `registry…/v2/…/manifests/…`，日志里约 620 ms 墙钟），
  直接测那条"镜像就绪探测"：解析缓存冷 **430–482 ms**、热 **1 ms**（缓存是进程内的
  `E2B_IMAGE_MANIFEST_TTL_S`，默认 60 s）。**不是登记/落盘慢** —— 那是可优化项（跨进程缓存解析
  结果，或由控制面解析一次把 digest 传给 worker）。

**② 活动沙箱的额外内存很小**

- **≈16 MiB / 沙箱**：4 个沙箱各跑一条常驻命令时，按**沙箱自己的宿主 uid** 汇总
  `/proc/*/VmRSS`，四个 uid 分别是 16 / 16 / 16 / 16 MiB，合计 **63 MiB**。
- 这就是"沙箱是进程、不是 VM"的直接体现：**没有 guest 内核、没有虚拟化进程**——那两样在
  microVM 方案里是每个沙箱都要付的固定内存。上面这个数只含该沙箱自己的进程
  （supervisor + 沙箱内进程），共享的镜像页缓存不重复计。
- 平台按 `memoryMB` 给每个沙箱做**准入预留**（默认 1 GiB，可调）：预留是配额口径，
  不是上面的实测占用——实测占用由用户负载决定。

**③ 沙箱内命令的执行延迟低**

- SDK 一次 `commands.run('/bin/echo ok')` 的**端到端往返**：p50 **102 ms** / p95 **113 ms**
  （n=20，从开发机经公网入口，含 SDK 的请求与流读回）；同一口径在**内网侧**实测
  **p50 ≈ 33 ms**（[docs/production-deployment-requirements.md](docs/production-deployment-requirements.md) §2.4.7）。
- 沙箱内一次 `stat`：镜像 rootfs 里的文件（节点本地）**30 µs** —— 这是"路径中介 + 内核"
  的真实成本；工作区里的文件 **2.3 ms**，大头是 NAS 往返而不是中介（同一个文件在快照/记账
  路径上用的是只取大小的 `statx`，实测 **0.01 ms**）。
- 隔离本身几乎不加钱：开 per-sandbox 网络命名空间后**建连 p50 0.291 ms**（未隔离 0.034 ms，
  只影响短连接）；在已部署的中介形态里再加 PID 命名空间，实测增量 **≤2 µs/次**。

> 复跑：`python deploy/scripts/acceptance/lightweight_metrics_probe.py`（内存那段需要
> `kubectl`，非 k8s 部署可加 `--no-memory`）。本次读数与探针输出：
> `tmp/k0s/lightweight-metrics.log`；其它口径出处见
> [docs/production-deployment-requirements.md](docs/production-deployment-requirements.md)
> §2.4.6 / §2.4.10 与 [docs/k8s-deployment.md](docs/k8s-deployment.md) §12 / §22。

> 适用范围：Linux（内核 6.12+，即 Landlock ABI ≥ 6）；暂停与快照是进程 / 文件系统级语义，
> 不保留运行内存。完整清单见 [§10 已知边界](#10-已知边界)。

- **兼容面速览**（协议细节见 [spec.md](spec.md)）：`Sandbox.create/connect/kill`、`commands.run` + PTY/stdin、
  文件 API、`health`/`metrics`/`logs`、Volume、Secret、`pause/resume`、`fork/snapshot`、
  network 策略（`allowOut`/`denyOut`/`rules`/`egressProxy`）、模板本地构建、MCP 网关；
  当前实现与 spec 的两处事实性偏差见文末。
- **隔离模型**：每个沙箱一个 **userns（身份翻译）+ pidns（看不见宿主）+ netns（只有 loopback，
  出口由 supervisor 代连）**，外面再套 Landlock 文件白名单与 seccomp 过滤器 —— 见
  [§2](#2-隔离边界三种命名空间)。

## 目录

1. [架构](#1-架构)
2. [隔离边界：三种命名空间](#2-隔离边界三种命名空间)
3. [仓库结构](#3-仓库结构)
4. [快速开始](#4-快速开始)
5. [核心概念](#5-核心概念)
6. [配置](#6-配置)
7. [测试与验收](#7-测试与验收)
8. [构建与发布](#8-构建与发布)
9. [文档索引](#9-文档索引)
10. [已知边界](#10-已知边界)

## 1. 架构

```text
e2b SDK（本仓库验证版本：Python 2.46.0 / JS 2.46.1）
   │  E2B_API_URL + E2B_SANDBOX_URL（生产里是同一个地址）
   ▼
┌──────────────────────────────────────────────────────────────────────┐
│ control-plane + gateway  :3000        （Deployment，可多副本 + Redis）│
│  · Sandbox REST API：注册表 / TTL / 资源准入 / 配额账本 / 调度        │
│  · 按 E2b-Sandbox-Id 把 Connect-RPC 与 /files 原样透传给目标 worker   │
│  · 自动扩缩容循环（autoscaler_service，按 pod-deletion-cost 缩 worker）│
└──────────────────────────────────────────────────────────────────────┘
   │  内部 API（X-Internal-Key）+ 节点身份校验（地址/源 IP）
   ▼
┌───────────────────────────┐        ┌──────────────────────────────────┐
│ e2b-worker（StatefulSet）  │        │ e2b-c3-agent（DaemonSet，每节点）│
│  envd_service :49983      │        │  face A :49985  uid 65534        │
│  非 root（uid 65534）      │  ────► │   · 写槽位 uid_map（身份授予）    │
│  · 沙箱生命周期/文件/命令  │        │  face B :49986  root             │
│  · Sandlock 执行器         │        │   · chown / rm / walk（文件步骤）│
│  · route B 槽位池          │        └──────────────────────────────────┘
└───────────────────────────┘
```

一次 `Sandbox.create()` 的调用链（生产形态）：

1. SDK → 控制面 `POST /sandboxes`；调度器选节点、预留配额、登记记录。
2. 控制面 → 目标 worker 的 `POST /agent/sandboxes`（内部 key）；worker 建工作目录
   （`0770 owner=<沙箱 uid> group=<worker gid>`）、挂卷、按需解包镜像 rootfs。
3. 第一条命令触发 **route B 槽位**：worker fork 一个子进程 → 子进程 `unshare(CLONE_NEWUSER)`
   → 把 `{sandbox_id, pid}` 报给控制面 → 控制面按自己的记录查出 uid 并指令本节点 agent
   **写 `uid_map`/`gid_map`** → 子进程 `setresuid(X)` 后 exec `sandlock-supervise`。
4. `sandlock-supervise` 在镜像 rootfs（chroot）里按 Landlock/seccomp 策略运行命令，
   路径中介负责网络策略、文件注入、配额记账。

三句话记住**谁有什么权限**（C3 硬规则，见 [docs/c3-privilege-relocation.md](docs/c3-privilege-relocation.md)）：

- **worker 零特权**：uid 65534、容器 BND 空集、镜像里没有任何 file-capability 二进制；
  它能动的是"自己作为属组的 `0770` 树"，和"点名一个 sandbox_id 请别人动手"。
- **agent 是唯一的特权组件**：每节点一个 DaemonSet，面 A（65534，`SETUID/SETGID` 写在 BND 里）
  只写 `uid_map`；面 B（root，`CHOWN/DAC_OVERRIDE`）只做 `chown`/`rm`/`walk`，
  且路径必须落在部署显式声明的根之下。
- **worker ↔ agent 不存在**：worker 只会拨控制面，控制面才会拨 agent。`envd_service/**`
  里不许出现 agent 的地址/端口/令牌（有单元测试钉住）。

## 2. 隔离边界：三种命名空间

沙箱不是一个容器、也不是一台 VM —— 它是**一个带 userns / pidns / netns 的进程**，
外面再套两层过滤器（Landlock 文件系统白名单 + seccomp 过滤器）。内核负责隔离，
平台只决定"谁在什么时候建哪个命名空间、谁有权写哪张映射"。

| 命名空间 | 开关 | 谁创建 | 买到什么 |
|---|---|---|---|
| **userns** | 形态自带（`E2B_PER_SANDBOX_UID`） | route B 槽位的子进程自己 `unshare(CLONE_NEWUSER)` | 身份翻译：**箱内 uid 0 ↔ 宿主侧沙箱池 uid** |
| **pidns** | `E2B_PID_NS`（部署清单全开，代码默认 `false`） | fork 的**中间进程**（先 userns，再 pidns） | 箱内看不见宿主与其他沙箱的 pid |
| **netns** | `E2B_ENABLE_NET_ISOLATION` + `E2B_FD_INJECT_CONNECT`（**必须成对**） | fork | 箱内只有 `lo`；出口由 supervisor 代连 |

### 2.1 userns：身份翻译，不是隔离

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
   `uid_map`/`gid_map`（[envd_service/slot_identity.py](envd_service/slot_identity.py)）；
3. 子进程轮询 `setresuid(X)` 直到成功，再 exec `sandlock-supervise`。

这也是为什么 route B 的槽位身份只能是 `agent-grant`：worker 自己既没有 `CAP_SETUID`，
也不该拥有"给一个进程安上任意身份"的能力。补充组在 `as_uid` 写 gid 映射时被
`setgroups=deny` 关掉，所以槽位进程保留的是 worker 的补充组（与旧路径一致）。

### 2.2 pidns：看不见别人，也看不见宿主

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
口径与实测表：[docs/production-deployment-requirements.md §2.4.10](docs/production-deployment-requirements.md)。

### 2.3 netns：只留 loopback，出口由 supervisor 代做

`E2B_ENABLE_NET_ISOLATION=1` + `E2B_FD_INJECT_CONNECT=1`（**两个必须一起给**）时，
每个沙箱在自己的网络命名空间里起来，里面只有 `lo`：

- **出站**：沙箱的 `connect()` 被 supervisor 接住，由 supervisor 在宿主侧建连，再把
  **已连接的 fd 注入**到沙箱自己的 socket fd 上 —— 被接住的 `connect()` 返回 0，
  CPython 的 socket 语义不变（不是靠 on-behalf 重写）。`allowOut`/`denyOut`/`rules`/
  `egressProxy`/SSRF 护栏都在这一步判定。
- **成对是硬要求**：只开 `E2B_ENABLE_NET_ISOLATION` 会让沙箱变成 loopback-only，
  故障表现为"网络超时"而不是报错，所以 `create_app` 启动期直接拒绝（
  `NET_ISOLATION_PAIRING_ERROR`）；确实想要不能出网的沙箱要显式声明
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
  逐项实测见 [docs/production-deployment-requirements.md §2.4.6](docs/production-deployment-requirements.md)。
- `E2B_ENABLE_NETNS` 是更早的 veth-pool 形态遗留旋钮，**默认关且对现在的形态不生效**；
  worker 启动时那套 veth 池 NAT（[envd_service/netns.py](envd_service/netns.py)）只在旧形态下才需要。

### 2.4 另外两层边界（不是命名空间）

- **Landlock**：文件系统访问白名单（`fs_writable` / `fs_mount` 落到策略里），
  这是"沙箱只能碰自己的树"的第一道；
- **seccomp**：worker 与沙箱各自一份过滤器。worker 必须真的跑在我们发的 profile 下
  （`E2B_REQUIRE_SECCOMP_FILTER` 会在启动时拒绝"没有过滤器"的容器），
  因为沙箱是从 worker 继承 syscall 面的；
- 另外，开启 `E2B_REAL_ROOT`（N35）时 fork 还会建一个沙箱自己的 **mount namespace**，
  在 `pivot_root` 进去之后**丢掉 `CAP_SYS_ADMIN`**，让内核自己去解析 `#!` 脚本与静态二进制
  （[docs/chroot-workspace-exec.md](docs/chroot-workspace-exec.md)）。

一句话：**userns 管"我是谁"、pidns 管"我看得见谁"、netns 管"我能连谁"，
Landlock/seccomp 管"我能碰什么、我能调什么"。** 四者叠加才是这个沙箱的边界；
它们都由同一份部署清单显式声明，且每一项都有回退开关。

## 3. 仓库结构

| 目录 | 内容 |
|---|---|
| `control_plane/` | 控制面：官方 Sandbox REST API、注册表（内存/Redis）、调度、准入、配额账本、网络策略校验、C3 agent 客户端、自愈、自动扩缩容循环 |
| `envd_service/` | 每节点 worker：Connect-RPC + 文件 HTTP 契约、沙箱生命周期、Sandlock 执行器、route B、卷/快照/检查点、MCP 网关、quota 监控 |
| `c3_agent/` | 每节点特权 agent：面 A（身份授予）/ 面 B（文件操作）+ `priv/` 下的 C 原语（`as_uid`、`e2b-maint`） |
| `quota_agent/` | 可选的配额服务端（在挂载 XFS 的机器上执行 `xfs_quota`，让非 root worker 也有每沙箱磁盘硬限） |
| `autoscaler/` | 扩缩容库（策略 + 状态 + `backends/k8s.py`），由控制面的循环调用 |
| `gateway_common/` | 控制面与 worker 共享的小工具：env、错误码、ID、keepalive、网络策略、路径、上传 |
| `deploy/` | Dockerfile（`docker/`）、k8s 清单（`k8s/`）、k0s overlay 与 apply/upgrade（`k8s-k0s/`）、生产 compose 栈（`stack/`）、示例 compose（`compose/`）、脚本（`scripts/`） |
| `tests/` | `unit/`（161 个文件）、`contract/`（65 个）、`sdk/{python,js}`、`security/`、`perf/` |
| `docs/` | 设计与运维文档（索引见 [§9](#9-文档索引)）；`docs/reports/` 是历史任务报告 |
| `third_party/sandlock` | sandlock 的 fork 子模块（构建 wheel 的源） |
| `wheels/fork/` | 构建产物（**不入库**），由 `deploy/scripts/build-sandlock-wheels.sh` 生成 |

## 4. 快速开始

### 4.1 本地开发（macOS，Local 执行器）

```bash
python3 -m venv tmp/venv
tmp/venv/bin/pip install -r requirements-test.txt

# 终端 1：控制面
E2B_API_KEYS=local-key tmp/venv/bin/python -m control_plane

# 终端 2：envd
E2B_EXECUTOR=local tmp/venv/bin/python -m envd_service
```

```python
import os
os.environ["E2B_API_URL"] = "http://localhost:3000"
os.environ["E2B_SANDBOX_URL"] = "http://localhost:49983"
os.environ["E2B_API_KEY"] = "local-key"

from e2b import Sandbox
sandbox = Sandbox.create()
print(sandbox.commands.run("python3 -c 'print(1+1)'").stdout)   # 2
sandbox.kill()
```

macOS 上只能跑 Local 执行器（Landlock/seccomp 是 Linux 特性）：协议兼容性可以这样验，
**隔离能力必须换 Linux**。合并形态下 `E2B_API_URL` 与 `E2B_SANDBOX_URL` 可以指向同一个端口
（见下一条）。

### 4.2 容器形态（compose）

```bash
cp deploy/compose/.env.example deploy/compose/.env      # 改密钥/端口/仓库
docker compose -f deploy/compose/docker-compose.prod.yml up -d --build
```

> 跑冒烟前先把每 worker 容量抬起来（出厂 `E2B_NODE_PROCESSES=256` 与单沙箱默认相等，
> 每个 worker 只能放 1 个沙箱，冒烟会报 `503 No resources available`）：
> `E2B_NODE_MEMORY_MB=4096`、`E2B_NODE_CPU_PERCENT=400`、`E2B_NODE_DISK_MB=8192`、
> `E2B_NODE_PROCESSES=1024`。

```bash
export E2B_API_URL=http://127.0.0.1:3000 E2B_SANDBOX_URL=http://127.0.0.1:3000 E2B_API_KEY=local-key
python deploy/scripts/multinode_smoke.py      # 跨节点分布 + 命令/文件/stdin + kill 后配额释放
python deploy/scripts/deployment_smoke.py     # 追加：跨 worker 迁移、共享卷隔离、模板构建→registry→worker
```

### 4.3 生产（k0s 集群）

本仓库的目标集群是**自建 k0s**（2 节点全 arm64，namespace `sandlock`）。
`deploy/scripts/open-cluster-tunnel.sh` 会建通道并自检集群身份：

```bash
deploy/scripts/open-cluster-tunnel.sh
export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"      # 任何 kubectl 都要带！

DRY_RUN=1 deploy/k8s-k0s/apply.sh 2>/dev/null | kubectl diff -f -   # 先看差异
deploy/k8s-k0s/apply.sh                                             # 渲染 + apply + 预热 base image
```

⚠ 本机 `kubectl` 的默认 context **不是**这个集群（指向另一套阿里云 ACK），敲写操作前务必
先跑上面的自检。集群身份、入口、上节点方式、现状与历次上线记录都在
[docs/deploy-clusters.md](docs/deploy-clusters.md)。

## 5. 核心概念

**模板与镜像缓存.** `E2B_BASE_IMAGE` + `E2B_TEMPLATE_IMAGES` 决定模板的 rootfs；模板配置了镜像后，
envd 用 Docker daemon 导出 rootfs 到 `E2B_IMAGE_CACHE_DIR`（缓存目录名带镜像 digest，
tag 更新自动落新目录），沙箱在 chroot 里执行。生产把缓存放共享卷（两层 GC：按量逐出 + 悬空清理）。
slim 镜像没有 `bash` 而官方 SDK 固定发 `/bin/bash`，执行器会自动回退 `/bin/sh`。

**route B 与 per-sandbox uid.** 每个沙箱有自己的宿主机 uid（`E2B_UID_POOL_START` 起的池），
工作区是 `0770 owner=<沙箱 uid> group=<worker gid>`：沙箱是属主，worker 靠属组做数据面
（文件 API、watcher、命令日志、快照、生命周期）。沙箱不是 worker 的同组进程，跨沙箱隔离
仍是内核 DAC。沙箱的路径中介（`sandlock-supervise`）由 route B 的槽位承载，槽位身份由
agent 授予（`E2B_SLOT_IDENTITY=agent-grant`，唯一取值）—— 身份翻译与命名空间的完整口径见 §2.1。

**暂停/恢复与检查点.** `pause` 冻结进程树（SIGSTOP/SIGCONT，不是内存快照）；
`pause(checkpoint=true)` 走控制面的 checkpoint 服务，把镜像打到平台状态目录，
换宿主机后仍能 `resume`。见 [docs/checkpoint-restore-e2b-half.md](docs/checkpoint-restore-e2b-half.md)。

**快照与 fork.** 文件系统级快照（复制沙箱目录 + 元数据，捕获期临时冻结保证一致），等价官方
`keep_memory=false` 的冷启动语义 —— **不保留运行中的进程/内存/socket**；`fork(count=N)`
从同一快照建 N 个独立沙箱，逐项独立成败。

**磁盘配额与记账.** 三条一起用：`E2B_MAX_TOTAL_DISK_MB`（控制面卷级台账 + 准入）、
每沙箱 `diskMB`（中介侧的写入闸门，超限 = 不能写、不冻结）、以及可选 quota-agent
（在挂载 XFS 的机器上执行 `xfs_quota`，给非 root worker 也能用的硬限）。
见 [docs/sandbox-disk-quota.md](docs/sandbox-disk-quota.md)、[docs/disk-quota-options.md](docs/disk-quota-options.md)。

**自动扩缩容.** 2026-09-30 起扩缩容是**控制面自己的一个任务**（`control_plane/autoscaler_service.py`，
`autoscaler/` 变成库），只缩 worker 的 StatefulSet，且缩容前必须确认目标 pod 真的会被
StatefulSet 删掉（只删最高序号且必须是"待淘汰候选"）。方案与阈值见 [docs/SCALING.md](docs/SCALING.md)。

**网络策略.** `allowOut`/`denyOut`（IP/CIDR + 域名）、`rules`（80/443 透明 MITM 按域名 ACL，
镜像 rootfs 模式下把临时 CA 拼进每沙箱信任副本）、`egressProxy`（fork sandlock 的 SOCKS5
on-behalf 隧道，代理不可达即 ECONNREFUSED，绝不回退直连）、`maskRequestHost` 与
`transform.headers`（改写 wire Host、注入凭据；secret 只落在 supervisor 侧）。
通配域名经每沙箱 loopback DNS 网关解析为合成 IP 再由 supervisor 代连 + SSRF 二次校验。
需要 worker 设 `E2B_ENABLE_NETWORK=true`。

## 6. 配置

最常用的一小撮（全量速查见 [docs/env-vars.md](docs/env-vars.md)，权威口径是各组件的
`config.py` 与 [spec.md](spec.md) §7.2）：

| 变量 | 作用 |
|---|---|
| `E2B_API_KEY` / `E2B_API_KEYS` | 控制面 API Key（`_KEYS` 是轮换窗口） |
| `E2B_BASE_IMAGE` / `E2B_TEMPLATE_IMAGES` | 模板 rootfs（不配就跑纯 Sandlock 形态） |
| `E2B_EXECUTOR` | `auto` / `sandlock` / `local`；`auto` 只在顶层包装不上时回落 `local` |
| `E2B_WORKSPACE_BASE` / `E2B_STATE_BASE` | 沙箱树根 / 平台自己的状态根（N27 起分离） |
| `E2B_PER_SANDBOX_UID` + `E2B_UID_POOL_START/_SIZE` | 每沙箱宿主机 uid 池（池必须避开 worker 自己的 uid/gid） |
| `E2B_PRIV_HELPER_TRANSPORT` | 文件步骤走哪条路：只接受 `auto`/`agent`（两者都是 agent）；`exec`/`socket` 已退役、启动期具名拒绝 |
| `E2B_ROUTE_B_TMP_ROOT` | 槽位文档（policy/program）所在根，**worker 与 CP 必须逐字一致** |
| `E2B_INTERNAL_API_KEY(_S)` | 内部组件共享凭据（`X-Internal-Key`） |
| `E2B_REDIS_URL` | 配了就多副本共享注册表/配额/迁移锁 |
| `E2B_QUOTA_AGENT_URL` | 配了就启用服务端配额（否则降级并打警告） |
| `E2B_IMAGE_REGISTRY`(+`_USERNAME`/`_PASSWORD`) | 模板镜像 push/pull 的仓库（不配则单机 OCI tar 形态） |
| `E2B_MAX_TOTAL_*` / `E2B_NODE_*` | 全局与节点级准入上限 |

## 7. 测试与验收

| 层 | 命令 | 环境 |
|---|---|---|
| L1 单元 + L2 契约 | `pytest tests/unit tests/contract` | macOS / Ubuntu |
| L3 Python SDK | `pytest tests/sdk/python` | Linux test runner（Sandlock）；macOS 可用 Local 执行器验协议 |
| L3 JS SDK | `pytest tests/sdk/js` | macOS / Linux（首次 `cd tests/sdk/js && npm install`） |
| L4 安全 | `E2B_BASE_IMAGE=python:3.14-slim pytest tests/security` | 内核 ≥ 6.12（Landlock ABI ≥ 6）+ Docker |
| L5 性能 | `E2B_BASE_IMAGE=python:3.14-slim pytest tests/perf --perf` | Linux + Docker，profile 落 `tmp/perf/` |

容器车道（复现部署形态，而不是用 `--privileged` 的"万能车道"）：

```bash
# 部署形态车道：非 privileged + seccomp=unconfined（+ 第二相：uid 65534 的 E5.1 形态）
docker compose -f deploy/compose/docker-compose.test.yml build
./deploy/scripts/test-prod-shaped.sh
```

macOS 宿主只看**容器运行时 VM 的内核**：`python3 -c "import sandlock; print(sandlock.landlock_abi_version())"`
必须 ≥ 6（OrbStack 实测 8；旧 Docker Desktop 常低于该值，那台机器就不能当 Sandlock 验收环境）。
跨平台/交叉架构的车道见 [docs/cross-platform-lanes.md](docs/cross-platform-lanes.md)；
直连远端部署跑 SDK 测试见 [docs/remote-testing.md](docs/remote-testing.md)。

跑车道/构建/部署**之前**先看一遍
[docs/build-test-deploy-pitfalls.md](docs/build-test-deploy-pitfalls.md)：里面是"看起来像代码坏了、
其实是环境/流程"的那些坑（镜像没重建、默认 seccomp 拦 `unshare`、缺 `CAP_SYS_PTRACE`、
冷缓存 428、引号地狱等）。

## 8. 构建与发布

```bash
./deploy/scripts/build-sandlock-wheels.sh     # 先出 wheels/fork/*.whl（不入库）
./deploy/scripts/build-and-push.sh            # 多架构构建 + 推 ACR，并把版本写进 deploy/stack/.version
export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"
deploy/k8s-k0s/apply.sh                       # 渲染 + apply + 等滚动 + 预热 base image
```

- 版本戳 = `git describe --tags` + 时间戳，k8s 清单按它 pin 每个镜像 tag。
- 镜像：`e2b-sandlock-control-plane-gateway`（控制面 + gateway 合并，单端口）、
  `-worker`、`-agent`、`-quota-agent`（可选）。**没有 autoscaler 镜像**。
- 升级顺序 **agent（DaemonSet）→ worker（StatefulSet）**，`apply.sh` 里有对应的 rollout 闸门；
  详见 [docs/k8s-deployment.md](docs/k8s-deployment.md) 与 [deploy/k8s-k0s/README.md](deploy/k8s-k0s/README.md)。
- 每次上线的版本、读数与当时的形态判定记录在 [docs/deploy-clusters.md](docs/deploy-clusters.md) §7。

## 9. 文档索引

**入口与现状**

- [docs/deploy-clusters.md](docs/deploy-clusters.md) —— 目标集群怎么连、里面有什么、历次上线记录（改部署前必读）
- [docs/k8s-deployment.md](docs/k8s-deployment.md) —— k8s 部署指南（清单逐项、升级/回退、能力集、故障）
- [deploy/k8s-k0s/README.md](deploy/k8s-k0s/README.md) —— 自建 k0s 的差异与运维脚本
- [docs/env-vars.md](docs/env-vars.md) —— 环境变量速查
- [docs/open-issues.md](docs/open-issues.md) / [docs/task-backlog.md](docs/task-backlog.md) —— 未完成问题与总清单

**设计与形态**

- [docs/c3-privilege-relocation.md](docs/c3-privilege-relocation.md) —— 控制面零特权 / agent 执行 / worker 跑沙箱（已实施）
- [docs/production-deployment-requirements.md](docs/production-deployment-requirements.md) —— 生产部署要求（能力集、存储、镜像源、配额）
- [docs/chroot-workspace-exec.md](docs/chroot-workspace-exec.md) · [docs/pure-shape-decision.md](docs/pure-shape-decision.md) · [docs/n14-retire-the-emulation.md](docs/n14-retire-the-emulation.md) —— 执行形态的选择与代价
- [docs/tenant-isolation.md](docs/tenant-isolation.md) · [docs/security-hardening.md](docs/security-hardening.md) —— 隔离与加固
- [docs/checkpoint-restore-e2b-half.md](docs/checkpoint-restore-e2b-half.md) · [docs/resource-contention.md](docs/resource-contention.md) · [docs/control-plane-multi-replica.md](docs/control-plane-multi-replica.md) · [docs/SCALING.md](docs/SCALING.md)

**磁盘与配额**

- [docs/sandbox-disk-quota.md](docs/sandbox-disk-quota.md) · [docs/disk-quota-options.md](docs/disk-quota-options.md) · [docs/disk-accounting-dirty-dirs.md](docs/disk-accounting-dirty-dirs.md) · [docs/n25-remainder-plan.md](docs/n25-remainder-plan.md)

**流程与历史**

- [docs/build-test-deploy-pitfalls.md](docs/build-test-deploy-pitfalls.md) · [docs/cross-platform-lanes.md](docs/cross-platform-lanes.md) · [docs/remote-testing.md](docs/remote-testing.md)
- [docs/reports/](docs/reports) —— 各任务的验收报告；[docs/HANDOFF.md](docs/HANDOFF.md) 与
  [docs/c2-ownership-frontload.md](docs/c2-ownership-frontload.md) 是历史/未实施的记录

## 10. 已知边界

- **Linux only**：Landlock/seccomp 是 Linux 内核特性，macOS 上只能跑 Local 执行器（协议兼容性）；
  验收环境看容器 VM 的内核（Landlock ABI ≥ 6，即内核 6.12+）。
- **暂停/快照不是内存快照**：`pause` 冻结进程树；快照/`fork` 是文件系统级的冷启动语义，
  运行中的进程、内存、socket 都不保留。
- **共享卷上的配额靠甲方**：每沙箱磁盘硬限在 NFS 上要由服务端执行（quota-agent 跑在挂载 XFS
  的机器上）；网络文件系统（NFS/CephFS）上的 `chown` 只能由 euid 0 做，所以这一步固定在 agent 面 B。
- **控制面默认单写**：多副本要 `E2B_REDIS_URL`；否则注册表/配额在进程内存里。
- **路由与迁移**：同一沙箱同一时刻只在一个节点运行（路由表保证）；迁移不搬运运行中的进程，
  共享存储模式下只重建挂载与路由。
- **仓库暂无 LICENSE 文件**：需要声明许可时请补一份（此前 GitHub 上的初始提交带过 Apache-2.0，
  但那不在本仓库历史里）。

## 与 spec 的两处事实性偏差

1. **`envdVersion` 用 `0.6.4+sandlock`**：spec 原文是 `0.6.4-sandlock`，但官方 SDK 用
   `packaging.Version` 解析该字段，`0.6.4-sandlock` 不是合法 PEP 440 版本号，会在 `Sandbox.create()`
   时抛 `InvalidVersion`；`0.6.4+sandlock` 合法且语义不变。
2. **SDK 版本**：本仓库按 spec 写定的版本测试 —— Python `e2b==2.46.0`
   （[requirements-test.txt](requirements-test.txt)）、JS `e2b@2.46.1`
   （[tests/sdk/js/package.json](tests/sdk/js/package.json)）。spec 提到的 Python `2.46.1` **在 PyPI
   上不存在**（2.46.x 只有 2.46.0 / 2.46.4）。写这份 README 时（2026-10-01）PyPI 与 npm 上的最新版
   都是 **2.51.0**，本仓库**未在 2.51 上验证**；换 SDK 版本前先跑 `tests/sdk/python` 与
   `tests/sdk/js` 两套。
