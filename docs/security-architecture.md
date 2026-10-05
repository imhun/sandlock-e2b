# 安全架构

给要评估这套东西的人看的。逐项证据在
[`security-audit/security-framework.md`](security-audit/security-framework.md)，
那篇是给接手的人查细节用的；这篇只讲三件事：**这套防线怎么设计的**、
**因此强在哪**、以及**边界在哪**。

一句话：**沙箱与宿主之间没有共享的信任**。判定链上任何一层被单独打穿，
上面还有别的；而通到宿主的只有最外层一道部署配置。

```mermaid
flowchart LR
    subgraph DEF["防护层 · 谁拦什么"]
        direction TB
        L1["<b>L1 准入</b><br/>API key · 配额 · 记账"]
        L2["<b>L2 令牌</b><br/>X-Access-Token → runtime registry"]
        L3["<b>L3 seccomp 内层</b><br/>blocklist 76 + arg 过滤 4<br/>EPERM"]
        L4["<b>L4 Landlock</b><br/>路径 + 设备 ioctl 粒度<br/>EACCES"]
        L5["<b>L5 中介层</b><br/>路径翻译 · /proc 合成<br/>netlink 虚拟 · 代执行"]
        L6["<b>L6 外层 profile</b><br/>默认动作拒绝<br/>ENOSYS(38)"]
        L7["<b>L7 网络策略</b><br/>worker↔agent 隔离"]
    end

    subgraph TOPO["部署拓扑 · namespace sandlock"]
        direction TB
        C["客户端"]
        GW["control-plane<br/><b>对外唯一入口</b>"]
        subgraph WPOD["e2b-worker pod<br/>uid 65534 · cap drop ALL"]
            SUP["supervisor"]
            SBX["沙箱<br/>独立 pid / net ns · uid 池内独占"]
            NAS[("共享 NAS")]
            SUP --> SBX
            SBX <--> NAS
        end
        APOD["e2b-c3-agent<br/>⚠ hostPID · 无 seccomp"]
        HOST["宿主机 kernel"]
    end

    C -->|"① HTTPS"| GW
    GW -->|"② 内部 API"| SUP
    WPOD ==> HOST

    L1 -.-> GW
    L2 -.-> WPOD
    L3 -.-> SBX
    L4 -.-> SBX
    L5 -.-> SUP
    L6 -.-> WPOD
    L7 -.-> APOD

    classDef layer fill:#1f2937,stroke:#60a5fa,color:#e5e7eb
    classDef svc fill:#14532d,stroke:#22c55e,color:#fff
    classDef sbx fill:#78350f,stroke:#f59e0b,color:#fff
    classDef agent fill:#7f1d1d,stroke:#ef4444,color:#fff
    classDef host fill:#1c1917,stroke:#a8a29e,stroke-width:3px,color:#fff
    classDef store fill:#374151,stroke:#6b7280,color:#f3f4f6
    classDef guard fill:#1e3a5f,stroke:#3b82f6,color:#fff
    classDef client fill:#374151,stroke:#6b7280,color:#f3f4f6
    class L1,L2,L3,L4,L5,L6,L7 layer
    class GW,SUP svc
    class APOD agent
    class HOST host
    class NAS store
    class SBX guard
    class C client
```

## 威胁模型

**前提：攻击者已经在沙箱内拿到任意代码执行** —— 任意命令、PTY、文件 API、任意 env、
任意 cwd、网络按策略。他读得着自己的整个 rootfs，可以无限次试。

在这个前提下分四类判定，编号与
[`attack-surface.md`](security-audit/attack-surface.md) 一致：

| | 目标 | 典型后果 |
|---|---|---|
| L1 | 沙箱 → 宿主 / worker | 逃逸，读到节点上别的租户 |
| L2 | 沙箱 → 另一个沙箱 | 横向，读别人的 workspace |
| L3 | 沙箱 → 平台面 | 越权，拿到控制面或 c3-agent |
| L4 | 打垮 worker / 节点 | 可用性 |

**不在模型内**：控制面 API key 泄露、节点 root 失陷、DNS 劫持。那是运维面的事，
把它们混进沙箱威胁模型会让优先级失真。

## 核心设计

七层各解一个问题，互不假设：

| 层 | 拦什么 | 谁执行 | 拒绝指纹 | 门禁 |
|---|---|---|---|---|
| **L1** 准入 | 谁能来 | control-plane | HTTP 401/429 | — |
| **L2** 令牌 | 来了能干什么 | envd 两半守卫 | 拒绝 | `test_envd_token_fail_closed.py` |
| **L3** seccomp 内层 | 能不能发起特权 syscall | 沙箱进程 | `EPERM` | `cargo test -p sandlock-core --lib` |
| **L4** Landlock | 能不能碰这个路径 | 沙箱进程 | `EACCES` | `protection.rs` 的 `*_deployed_abi` |
| **L5** 中介层 | 语义上该不该放行 | supervisor | `EAFNOSUPPORT` / `EOPNOTSUPP` | 同 L3 + `test_socket_families.py` |
| **L6** 外层 profile | 兜住内层没登记的面 | worker 容器 | `ENOSYS(38)` | `test_ioctl_inventory.py` |
| **L7** 网络策略 | worker ↔ agent | k8s NetworkPolicy | 连不上 | `test_priv_maint_worker_gate.py` |

三层的分工值得单独说，因为它们最容易被误读成"重复劳动"：

- **L3 按名字拒**（blocklist 76 条）+ **按参数拒**（arg 过滤器 4 处：`clone` 的
  `CLONE_NEW*` 位、`socket` 的 `SOCK_RAW`/`SOCK_DGRAM`、`ioctl` 的 20 个请求码、
  `prctl` 的 `PR_SET_DUMPABLE`/`PR_SET_SECUREBITS`/`PR_SET_PTRACER`）。
  `ioctl` 在 seccomp 里**没有请求码粒度**，JEQ 链是唯一有粒度的地方，所以那份清单
  列了什么就是全部 —— 而且**猜错请求码是静默失效的**（JEQ 永不匹配，表里看着有、
  实际不拒），`context/tests.rs` 把这些常量连同它们的存在一起钉住。
- **L4 按路径拒**，六项保护默认 `strict_all()`：内核 ABI 够不着就**拒绝建箱**，
  不静默降级。六项的 ABI floor 最高是 6，正好等于线上实测 ABI ⇒ 线上无降级
  （本地车道是 8，所以"floor 够不够得着"必须单独测，不能靠本地）。
- **L5 补前两者抓不到的语义**：`chroot` 形态下子进程的**内核根其实是宿主 `/`**
  （fork 不调 `chroot(2)`，靠路径翻译），所以"不在 `chroot_path_syscalls()` 里的
  带路径 syscall 会以宿主根为基准执行" —— `chroot` 自己就这样漏过一次。

### 判定链：为什么第四层是唯一宿主边界

把七层按"谁来执行"折成四段：**L3 / L4 / L5 是沙箱代码**（fork + envd），
**L6 是部署配置**。于是——

**打穿前三层只能到 worker 容器，打穿第四层才到宿主。**

这条换算就是整套架构的可推理性来源：前三层在仓库里、可单元测试、每次改动都有 diff；
第四层是一份 416 条的 JSON，它的失效模式是"换了运行时就没了"
（实测：`seccomp=unconfined` 下 7 个 `*at` syscall 全部到达内核，Docker 内置默认档放行
`fchmodat2`）。所以"改哪一层会破什么"是可回答的，而第四层是唯一值得当边界看的那层。

## 五个设计选择

### 一、纵深不靠"一层够用"

常见做法是选一个强机制（Landlock 或 seccomp）然后赌它没漏。这里是各层解决**不同性质**
的问题：准入（谁能来）、令牌（来了能干什么）、seccomp（能不能发起特权 syscall）、
Landlock（能不能碰这个路径）、中介（语义上该不该放行）。

层与层之间**不互相假设**。最有说服力的一次实测：内层 seccomp 有没有挡住
`fchmodat2`？**没有** —— 挡住它的是外层 profile，而外层一换就漏。任何一层单独被绕过，
上面还有别的。

### 二、fail-closed 是默认值，不是选项

Landlock 的保护默认 `strict_all()`：内核 ABI 够不着就**拒绝建箱**，不静默降级。
只有显式标成 `Degradable` 的保护才会悄悄丢掉对应的 mask 位。

令牌守卫同构 —— 早期版本是 `if runtime.access_token and token != ...`，
空 token 时**恒真**（整个逃逸的根因）。现在是
`if not runtime.access_token or token != ...`：拿不到 token 一律拒。

这类"默认宽松、需要显式收紧"的地方在这套设计里被系统性排除了。

### 三、隐藏信息用合成，不用拒绝

netlink 和 `/proc/net/*` 都不是"拒绝访问"，而是**返回生成的内容**：
`/proc/net/dev` 永远只列 loopback，netlink 的接口转储由 supervisor 合成。
`AF_NETLINK` / `NETLINK_ROUTE` 照常建得起来，`getifaddrs()` 和 `if_nameindex()`
照常能用 —— 但里面没有宿主接口名。

对比一刀切拒绝的方案：后者会让 `ip addr`、`if_nameindex()`、大量运行时和测试框架
在沙箱里直接报错。合成视图把"看不见"和"不能用"分开了，而且**不砸产品**。

同一个沙箱里两条信道口径可能不同（netlink 跟 `net_isolation` 走，
`/proc/net/dev` 无条件 loopback-only），拿它们互相印证会量错。

### 四、每层拒绝有独特指纹

排障时能立刻定位到是哪一层拒的：

| 指纹 | 来自 |
|---|---|
| `EPERM` | 内层 blocklist |
| `ENOSYS(38)` | 外层 profile 的默认动作 |
| `EACCES` | Landlock |
| `EAFNOSUPPORT` | 中介层的语义拒绝（沙箱化的正常报错，不是权限） |
| `EOPNOTSUPP` | 中介层的路径语义 |

后两个尤其重要：`EAFNOSUPPORT` 用的正是内核对未知 family 的标准码，
所以沙箱里的报错和"平台真的不支持"无法区分，不会把正常错误读成权限事件。

> 口径提醒：`ENOSYS(38)` 是**实测**值（Docker 29.4 / x86_64 车道，
> 未列入 profile 的号一律 38），但它由**运行时**给出，**不是**文件里写的 ——
> `sandlock-worker.json` 没有 `defaultErrnoRet`，而显式加 `defaultErrnoRet: 1`
> 或 `: 38` 都不改变读数（三种取值实测同一条）。文件里唯一的 `errnoRet: 38` 是
> `clone3` 那条（让 glibc 透明回退到 `clone(2)`）。
> 另外 **compose 车道上 Docker 会把它自己的内置默认 profile 合并进来**：
> `statmount` 就是例子（自定义 profile 里根本没提它，却稳定回 `EPERM`，
> 与"仅 Docker 默认档"读数一致）。所以这张表描述的是**生效后的合并过滤器**，
> 不是那份 JSON 单独的行为。

### 五、同一个属性由两道独立机制守

隐藏宿主接口名靠 **netlink 合成 + `SIOCGIF*` ioctl deny list**。
两者互不暗示 —— 这是它的风险，也是它的价值：删掉一个，另一个还在，
而且"删掉这个是不是多余"这个念头本身就说明注释写得不够。

同样的做法用在几处：终端写入族不加 deny，靠"每沙箱独立 devpts 实例 +
`/dev/tty` 打不开（实测 `ENXIO`）+ 沙箱内同 uid"三个前提；前提被破坏时测试变红，
提醒该把 deny 加回去。**它能攻击的面本来就不可达，加 deny 只会打断
`tmux`/`vim`/`ssh`** —— 第四轮审计原本要加，动手前查消费者后推翻了。

## 安全性优势

### 优势一：单点失效不等于失守

单机制方案（只 Landlock，或只 seccomp）把全部赌注押在一个组件没漏上。这里的证据是
不对称且可测的：L3 挡不住 `fchmodat2`，L4 挡不住内核里的算术错误，L6 换个运行时就没了。
**任何一层的强度都不足以单独承担边界**，所以"某一层有洞"不是待修的 bug，
而是设计里预留的冗余。

### 优势二：同层内还有独立冗余

关键属性不由单点把守：隐藏网卡名 = netlink 合成 + ioctl deny；沙箱文件隔离 =
Landlock + 每沙箱独立 uid（`0770` owner=沙箱 uid、group=worker gid，
沙箱 gid 永不等于 worker gid ⇒ 即使 Landlock 被绕过，DAC 仍是第二道墙）。

这带来一个反直觉但重要的性质：**改动通常不需要新的安全论证**。删掉一份 deny 之前，
先得论证另一份还在；这正是把冗余写进注释和测试的目的。

### 优势三：合成视图让隐藏信息零功能代价

多数方案在"不泄露"与"能用"之间二选一。这里用 supervisor 合成报文把两者拆开，
所以隐藏宿主拓扑**不必**以牺牲 `ip addr`、`if_nameindex()`、运行时与测试框架为代价。
对一个要跑真实工作负载的平台，这条直接决定可用性。

### 优势四：拒绝可归因，故障可排障

五组 errno 指纹互不重叠，差分实验（同一探针在 profile ON/OFF 各跑一遍）能把
"哪一层拒的"变成读数而不是推理。副作用同样重要：**中介层能用内核的标准错误码表达
"这件事被沙箱化了"**，于是沙箱里的正常报错不会被监控系统误读成权限事件。

### 优势五：绝大多数修复落在可测代码而非部署配置

判定链前三层是仓库代码，改动有 diff、有单元测试、能进 CI；第四层是一份 JSON。
把边界尽量往前压到代码侧，意味着安全性的维持靠的是常规工程流程，而不是
"记得别改那份 YAML"。

### 优势六：共享内核路线的取舍是显式的

沙箱与宿主共用内核（对比 Firecracker/Kata 的虚拟化层、或 gVisor 的用户态内核），
换来的是无设备模拟层、原生 syscall 与兼容性；代价是**遏制必须来自 syscall 面与部署
配置，而不是另一个内核**。

这个取舍的代价是可命名的，不是抽象的：CVE-2026-53362（`__ip6_append_data()` 的
UDP corking 路径，CISA KEV 在列）就是它的样本 —— 有独立内核时这是被隔离的缺陷，
共用内核时它是**宿主 kernel 的缺陷**。

但要注意这条教训的形状：**"内核里的缺陷沙箱层管不了"不等于"没有沙箱层缓解手段"。**
这一例的触发点在 `sendmsg`/`splice` 上，而 sendmsg 本来就在沙箱的中介层里 ——
所以拦它是可以做的（见「能力边界」里那一项）。真正的判断标准不是"缺陷在哪一层"，
而是**触发它的那个 syscall 有没有落在沙箱能看见的地方**。落下了就有闸，没落下才只能打补丁。

### 优势七：文档与门禁同源

每个机制的结论都指向仓库里的测试，而不是一次性探针。没跑就是没跑。

## 能力边界

按"沙箱够不够得到"分，**不按严重度** —— 后者会把打不到的东西排到前面，误导优先级。

### 够不到，根子在内核：CVE-2026-53362

节点 kernel `6.12.0-211` 未打此补丁（修复在 6.12.95）。**这一项此前的记录三处都是错的**
（机制、缓解手段、命名归属），下面按实测重写。

#### 机制

缺陷在 **`__ip6_append_data()`（`net/ipv6/ip6_output.c`）**，UDP corking 的**发送**路径：
corked 报文跨分片边界时 `fraggap` 字节被计入 `datalen`/`pagedlen` 却没计入 `alloclen`，
于是一次线性拷贝越界 **15 字节**写进 `skb_shared_info`。

它在**调用进程的上下文**里、由 `sendmsg(2)`/`splice(2)` 驱动 ——
**不在 softirq，也不在重组路径上**。这个区别决定了下面两件事。

#### 可达性：实测过的条件链

| # | 条件 | 状态 |
|---|---|---|
| 1 | 沙箱内有代码执行 | ✅ 威胁模型前提 |
| 2 | 有 net 规则（否则 `socket(AF_INET6, SOCK_DGRAM)` → `EPERM`） | 取决于沙箱配置 |
| 3 | **`splice(2)` 把 pipe 灌进 UDP socket** | ✅ 可达，且**不在 blocklist、不被中介层拦** |
| 4 | 出口设备有 `NETIF_F_SG` | ❓ **无法安全实测**（见下方纠正） |
| 5 | 出站有可用 IPv6 路由 | ✅ **实测：这才是真正卡住它的一条** |
| 6 | corked 报文总量 > 路径 MTU（跨分片边界） | 可用大包达到（`lo` MTU=65536、`eth0` MTU=1450） |

条件 4 是**必要**的：没有 `NETIF_F_SG` 时内核会静默丢掉这个 flag
（`flags &= ~MSG_SPLICE_PAGES`），paged 分支根本不走。

> **纠正一条此前的错误推断**：曾据 `/sys/class/net/*/flags` 读出 `lo=0x9`、
> `eth0=0x1003` 便断言"两者都没有 `NETIF_F_SG`"。**那是错的** —— 该文件是
> **IFF_\*** 设备标志（UP/BROADCAST/MULTICAST/LOOPBACK），**不是 `NETIF_F_*` 特性**，
> 那个 bit 根本不在其中。而 `NETIF_F_*` **没有对应的 sysfs 文件**，`ip link` /
> `RTM_GETLINK` 也只报 `ifi_flags`(IFF_*)。
> **结论：条件 4 无法在生产节点上安全测量** —— 唯一能确认它的办法是真正走一遍 paged
> 分支，也就是触发这个 bug。（环回按内核特性通常不带 SG，所以此前那批打 `::1` 的
> 一次性探针大概率确实退化成了普通拷贝 —— 但这是**推断，不是实测**。）

#### 缓解手段：不止打补丁

**触发面有一个用户态入口，而沙箱没在那里设闸**：

- `MSG_SPLICE_PAGES` 是**内核内部 flag**，在 `__sys_sendto` / `____sys_sendmsg` 入口就被
  `flags &= ~MSG_INTERNAL_SENDMSG_FLAGS` 清掉 ⇒ **用户态设不上**（实测：非页对齐 buffer
  带该 flag 发送仍然成功，若 flag 生效这里必然失败）。
- 用户态唯一能碰到的入口是 **`splice(2)`**（实测 `splice(pipe → udp socket)` 成功）。
- 而 `splice` / `vmsplice` **都不在 blocklist**，`network/mod.rs` 的代执行只覆盖
  `connect` / `sendto` / `sendmsg` / `sendmmsg` —— **`splice` 从旁边直接走过去**。

所以拦 `splice(2)` 到 socket 目标就能切断触发链，**不需要改内核**。不过"代价接近零"
只说对了负载兼容性那一半，而且**判据要收窄** —— 2026-10-05 用计数探针
（`deploy/scripts/acceptance/probe_splice_usage.sh`，跑在本机容器里，同一镜像同一夹具、
只差 seccomp 规则）逐类量过：

| 负载 | splice | vmsplice | sendfile | copy_file_range | 一刀切拦掉后 |
|---|---|---|---|---|---|
| 文件/管道：`cp -r`、`tar\|tar`、`cat\|cat`、`tee`、`dd`、`gzip`、`sha256sum` | 0 | 0 | 0 | 0–400 | 不受影响 |
| Python/Node 标准库复制与下载（`shutil.copyfile`、`os.sendfile`、`socket.sendfile`、`fs.copyFileSync`、stream、`http.server`） | 0 | 0 | 0–33 | 0–2 | 不受影响 |
| `git add/commit/clone/checkout`、`curl -o` | 0 | 0 | 0 | 0 | 不受影响 |
| Go 文件复制 / `http.FileServer` | 0 | 0 | 74 | 2 | 不受影响 |
| **Go `io.Copy` socket↔socket（TCP 转发/端口桥）** | **29** | 0 | 0 | 0 | **断**（broken pipe；ENOSYS 与 EPERM 都不回退） |
| Python `os.splice`（显式调用者） | 1025 | 0 | 0 | 0 | 断（预期） |

三条结论：① 清单里"负载是 `node`/`python3`/`git`/`curl`、项目自身 0 次调用"这半**被实测支持**
—— 这些路径的零拷贝全走 `copy_file_range`/`sendfile`；② 判据**不能写成"fd_out 是 socket"**
—— Go 的 TCP 转发就是 `socket→pipe→socket`，按这条会一起打断，要放它过去得收窄到
**数据报 socket**（本 CVE 是 UDP corking，TCP 不走 `__ip6_append_data`）；③ 因此它**做不成纯
seccomp 规则**（seccomp 参数过滤拿不到 fd 类型），得进 `network/mod.rs` 的代执行 —— 代价是
每次 `splice` 一次 supervisor 往返（本仓库量过同类开销 +80~90 µs/次），这才是"代价接近零"
没写出来的那半。`vmsplice` 不必单列：它只能把用户页灌进 pipe，拦"pipe→数据报 socket 的
`splice`"已经切断整条链（顺带一个实测：Python **没有** `vmsplice` 包装，只有 C/Rust/Go 调得到）。

CIQ 列的另一条缓解 `user.max_user_namespaces=0` 在这里**不可用** —— 沙箱本身靠 userns 建立。
`esp4`/`esp6` 模块屏蔽对本 CVE 无效（不走 ESP 路径）。**打补丁仍然有效。**

#### 线上实测（2026-10-04，k0s 集群，只读）

| 项 | 读数 |
|---|---|
| `net.ipv6.conf.all.disable_ipv6` | `0` —— IPv6 栈**开着** |
| `/proc/net/if_inet6` | 只有 **link-local** `fe80::3cf8:97ff:fe20:7dd6`（eth0）与 `::1`（lo）。**无全局 IPv6 地址** |
| `/proc/net/ipv6_route` | 仅 link-local + 组播，**无 `::/0` 默认路由** |
| `udp6 sendto fe80::1%eth0` | `ENETUNREACH` |
| `udp6 sendto ::1` | **成功**（裸内核层，无 sandlock 策略） |
| `socket(AF_INET6, SOCK_DGRAM)` | **成功** |
| MTU | `lo`=65536、`eth0`=1450、`tunl0`=1480 |
| worker 生效的 `E2B_NETWORK_DENY_CIDRS` | **15 条，含 `::1/128`**（该变量在 k8s 里**未设**，由 `config.py::_network_deny_cidrs()` 回落到内置 `DEFAULT_NETWORK_DENY_CIDRS`；**只有显式置空才关闭保护**） |

**结论：真正卡住这条链的是条件 5 + 网络策略，不是条件 4。**

节点没有任何可路由的全局 IPv6，唯一能用的 IPv6 目的地是 `::1`；而 `::1/128` 在生效的
deny 清单里 ⇒ **默认配置的沙箱在 L7 就被挡住，压根到不了 paged 分支。**

两点要留着：

- 阻断点在**策略层**（L7），不在 syscall 或内核层。它挡的是"目的地"，不是"这个 flag"。
- **配置漂移会打开它**：显式设 `E2B_NETWORK_DENY_CIDRS=""` 会关闭整个保护（设计如此）；
  而 SEC-001 的补正正是"初版清单漏了 `0.0.0.0/8` 与 `::1/128`"——
  **任何还带着旧清单的部署，`::1` 这道就没有了**。

所以 `splice` 那道闸的价值是**纵深第二层**：它挡的是"flag 与 syscall 组合"，
与"目的地在不在 deny 清单"是两件独立的事。

#### 可利用性：触发 ≠ 提权

公开 exploit 链是 15 字节 OOB write → page UAF → dirty pagetable → 任意读写 →
覆写 `cred` → patch `avc_denied()` 绕 SELinux → 写 `core_pattern` 拿宿主 root，
**要求 x86_64 的 LA57**，CIQ 明确 **aarch64 不受影响**。

所以当前 arm64 集群上的准确结论是「**可触发、不可利用**」—— 既不是"安全"，
也不是"已被打穿"。仓库内没有任何自证提权的实验。

**但这不是设计安全，是运气**：同一套架构跑在 x86_64 上就是**实打实的可逃逸**
（RH：容器内可逃逸到宿主、绕过 SELinux、拿 root）。而公开 exploit 的两个前提
（"需要非特权 user namespace"、"需要容器内代码执行"）**正好都是这套架构提供的**。

一旦内核提权成功，容器侧加固（uid 65534、`cap drop ALL`）一律无效 —— 那已经是宿主
kernel 上下文的事。

#### 排期口径

理由是「**KEV 在列 + 触发入口在沙箱可拦却没拦**」，**不是**「已证实可提权」。
两件事都指向"尽快处理"，但可信度不同，别混用。

#### 一处命名错误待修

`third_party/sandlock` 的 `netlink/handlers.rs:47` 把本 CVE 与 "Dirty Frag"
并列成同一家族。CIQ 与 RH 均明确它是**独立的**（Dirty Frag = CVE-2026-43284 IPsec/ESP
与 CVE-2026-43500 rxrpc）。那是 submodule，未在此修改，仅记录。

**够得到，已挡，但机制是巧合**：`AF_RXRPC` 当前同时被 socket 白名单和"模块恰好没加载"
挡住，只有前者是策略。

**架构级已知项**：平台面无租户隔离（一个 API key 管所有沙箱）；
c3-agent 没有 syscall 过滤，只靠一条 NetworkPolicy ——
**这是当前架构最薄的一环，改一个策略就打通**。

（"共享 workspace 形态下磁盘配额不生效"那条**已在 2026-10-05 更正**：它记的是"没有中介的
pure 形态"，该形态已由 N15 中介化、N14 S5 T1 具名拒绝；集群六条写路径实测全部停在天花板。
仍然成立的边界是挂载卷走独立的卷配额线。见 `security-audit/findings.md` 的 OBS-5。）

**没做的**：跨切面冒烟没跑（验证面是 syscall 面与鉴权面，冒烟不覆盖这两者）。

## 怎么验证的

不是读代码得出的结论。每条都跑在真实部署上，包括集群外零凭据的端到端尝试。

门禁跟着层走：

| 改什么 | 跑什么 |
|---|---|
| fork 的 syscall 面 | `cargo test -p sandlock-core --lib`（除 2 个环境项外全绿） |
| fork 的 socket / netlink 面 | 同上 + `test_socket_families.py`（需沙箱车队） |
| Landlock 保护或 floor | 同上 + `protection.rs` 的 `*_deployed_abi` 测试 |
| envd 鉴权 | `test_envd_token_fail_closed.py` |
| c3 特权面 | `test_priv_maint_worker_gate.py` |
| 宿主接口名 | `test_ioctl_inventory.py` |

需要真实沙箱车道的（`tests/security/`）**没跑就是没跑**，不用别的套件绿了充数。

有几处专门防"本地绿 ≠ 生产"：每个 protection 的 ABI floor 与**线上实测的**
Landlock ABI 对照钉住（线上 6，六个 floor 最高也是 6，无降级），而本地车道是 8。

## 深入阅读

| 想看 | 去哪 |
|---|---|
| 每层机制、代码位置、门禁清单 | [`security-audit/security-framework.md`](security-audit/security-framework.md) |
| 逐轮审计记录与证据矩阵 | `security-audit/findings*.md` |
| 分层归因（哪些只靠外层挡） | [`security-audit/layer-attribution-2026-10-04.md`](security-audit/layer-attribution-2026-10-04.md) |
| 已修的高危项与修法 | `security-audit/remediation-SEC-R3-01.md` |
| 攻击面清单（L1–L4 逐项） | [`security-audit/attack-surface.md`](security-audit/attack-surface.md) |
| 加固历史 | [`security-hardening.md`](security-hardening.md) |
| 部署形态与上线记录 | [`deploy-clusters.md`](deploy-clusters.md) |
