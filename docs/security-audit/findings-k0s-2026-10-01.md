# k0s 线上沙箱白帽审计（第二轮）—— 发现（2026-10-01）

**范围**：自建 k0s 集群（2 节点 arm64、`v1.36.4+k0s`、namespace `sandlock`）上运行的
E2B 兼容沙箱，版本 `0.1.0-824-gf2aec0b-20261001-073534`。
**方法**：静态读码 + 通过官方 SDK / 控制面 HTTP 在**线上真实沙箱**里跑可复现探针；
结论一律以运行时证据为准。
**边界**：只确认与记录，**未修改任何生产代码、配置或部署**。

**生产影响披露**：本轮**没有**造成可见的生产影响 —— 9 个 pod 全部 `Running`、`restarts=0`，
终态 `/sandboxes` `[]`、`/volumes` `[]`。探针只创建/销毁一次性沙箱（单次 ≤ 65 个进程的
短时 `sleep`、限时 CPU 观测），未复现上一轮的 OOM。

与既有审计的关系：第一轮（`findings.md`，2026-09-16）测的是本地容器 + compose；
第二轮（`findings-k0s-2026-09-30.md`）测的是 k0s 的 L1 内核面 / 网络 / 认证 / 资源上限。
**本轮不重复上述覆盖**，聚焦两条被当成"设计如此"而从未从攻击者视角验证过的信任边界：
**租户可控的 `allowOut`** 与 **`allowPublicTraffic`**；外加一条非命名空间化的内核信息面。

| 编号 | 严重度 | 一句话 | 状态 |
|---|---|---|---|
| SEC-K0S-005 | **高** | `network.allowPublicTraffic=true` 关掉 **envd 整个表面**的访问令牌校验；实测**未认证** `process.Process/Start` 执行任意命令（exit 0、stdout 取回）。与 004 串联 ⇒ 跨沙箱未认证 RCE | 已确认，**未修** |
| SEC-K0S-004 | **中高** | 租户可控的 `allowOut` 无私有/集群网段过滤：`allow_out=["10.244.0.0/16"]` 即可从**沙箱内**打到控制面 3000、worker envd 49983、redis 6379（协议级 200） | 已确认，**未修** |
| SEC-K0S-006 | **中** | `sysinfo(2)`/`getcpu`/`statfs` 未命名空间化：监控过的 `/proc` 在"说谎"（1 GiB / load 0.00 / 4 进程），裸系统调用却是**宿主真值**（7.5 GiB / load 0.31 / 685 进程）+ 宿主运行时间。实测可观测**邻居沙箱**的进程数变化（686→701） | 已确认，**未修** |
| SEC-K0S-002 | 中 | 控制面 `/openapi.json`/`/docs`/`/redoc`（外加 `/`、`/healthz`）无需认证 —— 上一轮已记录，本轮**复核仍在**，且新增"沙箱内也能拉"（见 004） | 已确认，**未修** |

## 目标形态（先钉死）

| 项 | 实测值 |
|---|---|
| 版本 | `0.1.0-824-gf2aec0b-20261001-073534`（= `deploy/stack/.version`） |
| 内核 | `6.12.0-211.34.1.el10_2.aarch64`（宿主 Rocky；rootfs 是 Debian 13 trixie） |
| 沙箱内身份 | `uid=0(root) gid=0(root) groups=0,65534(nogroup)`（userns 内 fake root） |
| 根 | 真根：`/` xfs `ro`；`/workspace`、`/home/user` nfs `rw`；`/dev/{null,zero,urandom,tty,ptmx,pts}` |
| 隔离开关 | `E2B_PID_NS=true`、`E2B_ENABLE_NET_ISOLATION=true`、`E2B_FD_INJECT_CONNECT=true`、`E2B_ENABLE_NETWORK=true`、`E2B_REAL_ROOT=1` |
| worker | `runAsUser=65534`、`capabilities.drop=[ALL]`、seccomp `Localhost` |
| 入口 | 控制面 `http://172.18.78.49:3000`（同一份 `X-API-Key`） |

---

## SEC-K0S-005（高）`allowPublicTraffic` 关掉 envd 整个表面的访问令牌校验 ⇒ 未认证 RCE

### 事实

`allowPublicTraffic` 是**创建期由租户自己传的一个布尔**（`gateway_common/network.py`
的 `_CREATE_FIELDS` 含 `allowPublicTraffic`；SDK 侧是 `SandboxNetworkOpts.allow_public_traffic`）。
它的名字读起来像"把我自己的 HTTP 服务开放给公网"，**实际语义是"这颗沙箱的 envd 不再校验访问令牌"**：

```python
# envd_service/http/auth.py::require_http_sandbox（第 60-64 行）
if (
    not runtime.allow_public_traffic      # ← 只要这一位为 True，下面的令牌比较整条短路
    and runtime.access_token
    and token != runtime.access_token
):
    raise HttpAuthError(401, "Invalid access token")
```

同一条判定在 `envd_service/connect/router.py::_find_sandbox`（第 49-53 行）逐字复用 ——
也就是说 **`/files`（读写文件）与 Connect 一元/流式 RPC（`process.Process/Start` 等）一起放开**，
不只是"用户的业务端口"。而 envd 的判决点根本不是"公网"，`/files` 与 `/process.Process/Start`
本来就是**控制面 API**。

### 实测（从 worker pod 这个"沙箱外"的观测点发起，因此排除了"沙箱内自连被特殊对待"的可能）

对照组用的是**默认创建**、且已实测令牌校验生效的沙箱（`401 Invalid access token`）。

| 请求（`X-Access-Token` 留空） | 默认沙箱 | `allowPublicTraffic=true` |
|---|---|---|
| `GET /files?path=/workspace/proof.txt` | **401** `Invalid access token` | **200** `PROOF` |
| `GET /files?...` + `X-Access-Token: bogus` | **401** | **200**（错令牌同样放行） |
| `POST /process.Process/Start`（Connect 一元信封） | **401** `unauthenticated` | **执行成功**，见下 |

未认证命令执行的原始响应（`allowPublicTraffic=true`，无任何令牌，命令
`/bin/sh -c "id; echo HELLO-FROM-UNAUTH; hostname"`）：

```
HTTP 200
START:  {'pid': 3089}
STDOUT: uid=0(root) gid=0(root) groups=0(root),65534(nogroup)
STDOUT: HELLO-FROM-UNAUTH
STDOUT: sandbox-3076-1
END:    {'exitCode': 0, 'exited': True, 'status': 'exited', 'error': None}
```

即：**一个布尔开关，把"读文件 / 写文件 / 起进程 / PTY"整条 envd 控制面变成免认证**。

### 端到端复现：攻击沙箱 A → 受害沙箱 V，**零凭据**执行命令

最后把链条跑通一次（探针 `tmp/audit2/p21_cross_rce.py`）：

* 受害 **V**：`Sandbox.create(network={"allow_public_traffic": True})`，落在 `e2b-worker-1`；
* 攻击 **A**：`Sandbox.create(network={"allow_out": ["10.244.140.26", "10.244.192.227"]})`，
  **没有 V 的任何令牌**；
* A 内发 `POST http://<worker>:49983/process.Process/Start`，头里只有 `E2b-Sandbox-Id: <V>`，
  **无 `X-Access-Token`**：

```
w1 200 ["START {'pid': 3108}",
        'STDOUT uid=0(root) gid=0(root) groups=0(root),65534(nogroup)',
        'STDOUT CROSS-SANDBOX-UNAUTH',
        'STDOUT sandbox-3095-1',
        "END {'exitCode': 0, 'exited': True, 'status': 'exited', 'error': None}"]
```

即 **"任一沙箱 + 目标 id"即可在另一个沙箱里以 root 执行任意命令**，全程不需要受害者的令牌。

### 影响链（与 SEC-K0S-004 串联才是完整攻击面）

1. 租户（或其被入侵的工作负载）创建/演化的沙箱带 `allowPublicTraffic: true`；
2. **SEC-K0S-004** 让**任意其他沙箱**能把这些 worker 的 49983 端当作可路由目标（`allow_out` 写
   一个集群 CIDR 即可）；
3. 沙箱 id 是**可枚举的**（`GET /sandboxes` 对任意 key 全量返回；当前单租户模式下每个 key 都能列），
   于是"知道 id + 能到 49983"= **跨沙箱未认证 RCE**。

> 当前是单租户部署（OBS-6），所以第 3 步的"跨租户"要等运维触发条件成立；但**"跨沙箱"今天就成立**，
> 且它把 OBS-6 从"信息面越权"抬成了"远程执行"。

### 根因与建议修法（未实施）

根因是**把一个网络可见性开关复用成了认证开关**，且作用面是 envd 全部端点。
建议（任选，按性价比排序）：

1. **最小**：`allowPublicTraffic` 只放开"用户业务端口"的转发，**不放开 envd 控制面**；
   即 `require_http_sandbox` / `_find_sandbox` 的短路条件去掉 `runtime.allow_public_traffic`
   （envd 一律要令牌），把"公网可达"表达在别的层（入站转发/网关），不要表达成"免认证"。
2. 若必须保留（对齐官方 E2B 语义），至少把它**收窄到读**：`/files` 读放行、写与
   `process.*`/`filesystem.*` 仍要令牌 —— 因为"公开可读"与"公开可执行"是两回事。
3. 无论选哪个，都应加一条**回归**：对 `allowPublicTraffic=true` 的沙箱，未带令牌的
   `POST /process.Process/Start` 必须**非 2xx**（当前会执行）。

---

## SEC-K0S-004（中高）租户 `allowOut` 无私有/集群网段过滤 ⇒ 沙箱内可打控制面 / envd / redis

### 事实

`gateway_common/network.py::sandlock_network_policy` 的 docstring 把这件事写得很直白：

> ``private_deny_cidrs`` only affects the *implicit* full-egress branch ...
> **Explicit ``allowOut`` entries are never filtered here, so a caller that deliberately grants
> a private IP/CIDR keeps it.**

`DEFAULT_NETWORK_DENY_CIDRS`（`envd_service/config.py`，含 `10.0.0.0/8`、`172.16.0.0/12`、
`169.254.0.0/16`、`::1/128` 等）**只在"隐式全放行"分支**生效；显式 `allowOut` 走 `_to_net_allow`，
把条目原样变成 `tcp://<entry>:*`，**不做任何私有段判断**。

于是租户可以用自己的 `network.allowOut` 把集群内网"指"出来。（第一轮 SEC-001 修的是**隐式分支的
拒绝清单** —— 这条是**另一条分支**，修 SEC-001 碰不到它。）

### 实测

先证明 allowlist 真的生效（否则"打不到内网"可能是"什么都打不到"的空结论）：

| 请求（沙箱内） | 结果 |
|---|---|
| 默认创建：`pypi.org:443` / `github.com:443` | **CONNECTED**（内置固定域名集） |
| 默认创建：`example.com:443` | name resolution 失败（不在集合里，被 DNS 网关拒） |
| `allow_out=["example.com:443"]`：`example.com:443` | **CONNECTED**；同沙箱 `pypi.org` 反而 FAILED ⇒ allowlist 是**真·白名单** |

然后在"白名单为真"的前提下，把内网 IP/CIDR 放进去：

| `allow_out` | 沙箱内目标 | 结果 |
|---|---|---|
| `["10.244.140.26"]` / `["10.244.140.26:49983"]` | worker-0 envd `:49983` | **TCP CONNECTED** |
| `["10.244.0.0/16"]` | 控制面 `:3000` `/openapi.json` | **200 OK**（返回 3.1.0 规范） |
| `[<cp ip>]` | 控制面 `:3000` `/` | **200** `{"status":"ok","service":"e2b-sandlock"}` |
| `[<cp ip>]` | `:3000` `/healthz` `/docs` `/redoc` `/openapi.json` | 全部 **200** |
| `[<cp ip>]` | `:3000` `/sandboxes` `/internal/*` `/secrets` … | 401（**鉴权仍生效**，不是越权） |
| `[<redis ip>]` | redis `:6379` | **TCP CONNECTED**（`PING` 无应答 ⇒ 仍需 `AUTH`） |

沙箱内直读控制面的证据（`allow_out=[<cp ip>]`，`http.client`）：

```
/openapi.json  -> 200 OK  server=uvicorn
  {"openapi":"3.1.0","info":{"title":"E2B Sandlock Gateway - Control Plane",...
/docs          -> 200 OK   /redoc -> 200 OK   / -> 200   /healthz -> 200
/sandboxes     -> 401 {"code":401,"message":"Unauthorized"}
/internal/fleet/metrics -> 401
```

### 影响

* **网络分段被租户单方面移除**：租户工作负载与平台控制面（control-plane）、数据面（worker envd）、
  redis 之间原本"不可路由"，现在只要在请求里写一个 IP/CIDR 就通了。
* **把 SEC-K0S-002 抬了一档**：`/openapi.json`（47 条路径全图）+ 可交互的 `/docs` 现在**从不可信代码里
  就能拉**，不再依赖"能连到入口端口"。
* **把任何控制面/envd 的鉴权或解析缺陷变成"沙箱可达"**：C3 agent 的两条特权中继端口
  （49985/49986）虽然被 NetworkPolicy 挡住（实测连接**挂起**，不返回），但控制面 3000 与
  worker envd 49983 是**放行**的。
* 与 SEC-K0S-005 的组合见上节。

### 建议修法（未实施）

显式 `allowOut` 至少要与隐式分支同一把尺子：
1. 在 `_to_net_allow`/`normalize_network_config` 对 `allowOut` 里的 **IP/CIDR** 判定私有/环回/
   链路本地/集群 CIDR 并**拒绝**（或者允许但强制同时叠加 `net_deny=DEFAULT_NETWORK_DENY_CIDRS`，
   让 deny 优先）；域名条目要防"解析到内网"（下发时 pin，或按解析结果二次判定）。
2. 若产品上确实要支持"沙箱访问自建内网服务"，应当**逐条目显式白名单 + 审计日志**，
   而不是一个不设限的字符串列表。

---

## SEC-K0S-006（中）非命名空间化系统调用泄露宿主状态，并构成跨租户侧信道

### 事实

`/proc` 是**中介合成**的（`/proc` 只有 `.`/`..`；`/proc/self/status` → EACCES；
`/proc/meminfo` 报 1 GiB、`/proc/loadavg` 报 `0.00 0.00 0.00 4/4`）。**但底层系统调用没有一起被管**：

| 量 | 中介 `/proc`（沙箱被喂的值） | 裸系统调用（宿主真值） |
|---|---|---|
| 总内存 | `MemTotal: 1048576 kB`（1 GiB） | `sysinfo.totalram = 7468 MiB` |
| 空闲内存 | `MemFree: ~1015805 kB`（几乎全空） | `sysinfo.freeram = 230 MiB` |
| 1 分钟负载 | `0.00` | `sysinfo.loads[0] = 0.31`（真·宿主负载） |
| 进程数 | `4/4` | `sysinfo.procs = 685` |
| 运行时间 | `/proc/uptime` 不存在 | `sysinfo.uptime = 1611295 s`（≈18.6 天） |
| 内核 | rootfs 是 Debian 13 | `uname` = `6.12.0-211.34.1.el10_2.aarch64`（宿主 Rocky 内核） |
| 磁盘 | — | `statfs("/")` = XFS 99.7 GiB / 已用 31.6%；`/workspace` = NFS 10 PiB（NAS 聚合容量） |
| CPU | `/proc/cpuinfo` 不存在 | `getcpu` → `cpu=1`（宿主 CPU 号） |

同一台机上"合成值"与"真值"**相差一个数量级**，说明合成层是刻意做的，只是**没覆盖到 syscall 面**。
`sysinfo`/`getcpu`/`statfs` 在 `DEFAULT_BLOCKLIST_SYSCALLS` 与通知表里都不存在（`get_mempolicy`
那批在第一轮被禁了，但 `sysinfo` 没在那一批里）。

### 实测：这是一条**活的跨租户侧信道**（不是纯理论）

两个沙箱：观察者 O 每 0.3 s 采样一次 `sysinfo`；邻居 B 起 60 个短时 `sleep`（**无内存压力**、限时 6 s）。

```
基线 procs : [686, 686, 686, 686, 686, 686, 686, 686]
峰值 procs : 701                      ← 邻居 B 的进程被 O 看见
尾部 procs : [684, 684, 683, ...]
freeram    : 215 - 242 MiB            ← 宿主空闲内存的抖动能反推邻居活动
```

即：**沙箱能观测其他租户的活动**（起停进程、内存与负载波动），也能拿到宿主的绝对资源画像
与精确内核版本 —— 后者对"选哪个内核 CVE 打"是直接有用的。

### 影响

* 侧信道：共租户检测、活动指纹、资源画像；在"沙箱实例位置不可见"的前提下把宿主暴露出来。
* 信息泄露：宿主内核版本/发行版、磁盘容量与占用、NAS 聚合容量、CPU 号。
* 与 OBS-6（单租户）叠加时是"宿主画像"；一旦开放第二个使用者，直接变成跨租户观测。

### 建议修法（未实施）

与第一轮 `get_mempolicy` 的处理同类：把 `sysinfo`（arm64 179）、`getcpu`（168）加进沙箱
默认拒绝集，`statfs` 则需要在路径面上按"虚拟根内"翻译（它和 OBS-2 的 `inotify`/`getxattr`
是同一类"Landlock 覆盖不到的带路径 syscall"）。注意 `sysinfo` 是 glibc/运行时常用的
（`os.sysconf`、部分 GC/JIT），**优先做成"按受限值中介"而不是直接拒绝**，否则会表现为"工作负载坏了"。

---

## 复核：SEC-K0S-002 仍在（且新增"沙箱内可达"）

```
$ curl -s -o /dev/null -w "%{http_code}\n" http://172.18.78.49:3000/openapi.json   # 200
```

从**沙箱内**（SEC-K0S-004 的 `allow_out`）同样 200；`/`、`/healthz`、`/docs`、`/redoc` 也 200。
控制面真实业务端点全部 401，**性质仍是信息泄露，不是越权**。修法同上一轮的一行：
`control_plane/app.py` 的 `FastAPI(...)` 补 `docs_url=None, redoc_url=None, openapi_url=None`。

---

## 已实测为**干净 / 不可利用**的面（防止下轮重复劳动）

**1. `clone3` 的命名空间位——代码缺口在，但部署形态打不穿（复核结论与上一轮一致，依据更硬）**

上一轮记录"`clone3` 的命名空间位没有一层 sandlock 校验，但外层 profile 把 `clone3` 整条 deny 成
`ENOSYS`"。本轮用**正确的 `clone_args` 布局**（`flags,pidfd,child_tid,parent_tid,exit_signal,stack,
stack_size,tls,set_tid,set_tid_size,cgroup`，88 B）实测：

| 调用 | 结果 |
|---|---|
| `clone3(exit_signal=SIGCHLD)`（size 88 / 64） | **成功**（返回子 pid）—— 所以 `clone3` 本身**没有被整条 deny** |
| `clone3(SIGCHLD｜CLONE_NEWUSER)`、`NEWNS`、`NEWNET`、`NEWPID` | 全部 **EPERM(1)** |
| `clone(220)` 裸调用 | 成功 |
| `clone(CLONE_NEWUSER)` / `clone(CLONE_NEWNS)` | **EPERM**（sandlock 参数过滤，符合设计） |
| `unshare` / `setns` | **EPERM** |

结论：**没有拿到任何新命名空间**，因此"用 `clone3` 绕过 `clone` 的命名空间位过滤"这条**当前不可利用**。
但请注意防护来自**容器 profile 的条件规则**（`clone3` 本身可达、只有带 ns 位时被拒），
不是来自 sandlock 自己 —— 与代码注释宣称的"clone3 由 BPF 参数过滤兜住"核对后，真正的兜底是外层。
**残留风险**：换一台 profile 更宽的宿主即成立；修法仍是上一轮那条（在 `handle_fork` 里对
`clone3` 复用 `clone_flags()` 判定）。

**2. 跨沙箱 envd 访问：令牌边界是真的**

受害沙箱 V 放一个文件；攻击沙箱 A 用 `allow_out` 指到 worker 后，**用 V 的 id + 空令牌 / 错令牌**
请求 `GET /files` → **全部 401 `Invalid access token`**（8/8）。跨沙箱要拿 V 的真令牌才行。
（附注：同一沙箱"自连"自己的 envd 时，实测**即使令牌为空或错误也返回 200** —— 这一条本轮只记录
**现象**、未进一步定位机制（最可能是 worker 代连路径替它带了令牌，但未验证）；对安全的结论
以上一条"跨沙箱必须持真令牌"为准。）

**3. 其余复核读数**

| 项 | 实测 |
|---|---|
| 沙箱 `env` / `/proc/self/environ` | 只有 `PATH`/`PWD`；environ EACCES ⇒ **无令牌泄露** |
| 根 fs 写入 `/`、`/etc` | `Permission denied`（`ro`） |
| `mknod` | `EPERM` |
| `/workspace` 上设 setuid 位 | **成功**（`-rwsr-xr-x`）—— 但沙箱已是 userns fake root、NFS 通常 `nosuid`，**未观察到提权效果**，仅记录 |
| `/etc/shadow` | 可读，但只有镜像自带的锁定哈希（`root:*:…`），非宿主口令 |
| `/tmp` | **不可写**（`drwxr-xr-x nobody`）—— 功能面问题，与安全无关 |
| 内核 6.12 新系统调用 | `mseal`/`cachestat`/`fchmodat2`/`*xattrat`/`file_getattr,setattr` → **ENOSYS**（外层 profile）；`statmount`/`listmount`/`open_tree_attr` → **EPERM**（内层黑名单）；`open_tree`/`fsopen`/`fsconfig`/`fsmount`/`move_mount`/`fspick`/`mount_setattr`/`quotactl_fd`/`process_madvise`/`process_mrelease`/`kcmp`/`io_uring_setup`/`userfaultfd`/`bpf`/`perf_event_open`/`keyctl`/`get_mempolicy`/`memfd_secret`/`pidfd_getfd` → **EPERM** |
| 刻意留活 | `memfd_create`、`pidfd_open`、`pidfd_send_signal`、legacy `clone`、`landlock_*`（沙箱自己也用）读数不变 |

---

## 复现方式

探针在 `tmp/audit2/`（gitignored，不入库）。共同前置：

```bash
cd <仓库根>
export E2B_API_URL=http://172.18.78.49:3000 E2B_SANDBOX_URL=http://172.18.78.49:3000
export E2B_API_KEY=$(cat tmp/audit/api_key)
export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"     # 只有"从 worker pod 观测"那几条需要
```

| 探针 | 覆盖 |
|---|---|
| `p01_recon.py` | 环境/挂载/procfs/sysfs 全量侦察 |
| `p02_hostinfo.py` | `sysinfo`/`getcpu`/`statfs`/rlim`/`/proc` 对照（SEC-K0S-006 主体） |
| `p03_sidechannel.py` | 双沙箱侧信道（邻居进程数可见） |
| `p04`–`p07_*.py` | `allowOut` 机制验证 + 内网 IP/CIDR（**注意 SDK 的键是 `allow_out` 不是 `allowOut`**，用错会静默变成"无 network 字段"） |
| `p08`,`p09`,`p10` | 内网服务协议级可达性（控制面 / worker envd / redis / c3 agent） |
| `p11_public_and_cidr.py` | CIDR `allow_out` → 控制面 200；`allowPublicTraffic` 免令牌 |
| `p12`–`p14_*.py` | `allowPublicTraffic` 免认证 `/files`；跨沙箱令牌边界（401） |
| `p15_cp_paths.py` | 沙箱内扫控制面路径（SEC-K0S-002 复核） |
| `p16_syscalls.py` / `p17_clone3.py` | 新系统调用清单 + `clone3` 命名空间位 |
| `p18_final.py` | env/根 fs/mknod/setuid/shadow/rlimit 收尾 |
| `p19_pubtraffic_auth.py` / `p20_unauth_rce.py` | **SEC-K0S-005 的决定性证据：未认证 `process.Process/Start` 执行任意命令** |
| `p21_cross_rce.py` | **SEC-K0S-005 端到端：攻击沙箱 → 公开流量沙箱，零凭据命令执行（exit 0 + stdout）** |

## 遗留 / 优先级建议（未实施）

1. **SEC-K0S-005（最高）**：`allowPublicTraffic` 不得关掉 envd 认证；至少把写与
   `process.*`/`filesystem.*` 收回来，并加"免令牌 Start 必须非 2xx"的回归。
2. **SEC-K0S-004**：显式 `allowOut` 的 IP/CIDR 走与隐式分支同一套私有段判定（或强制叠 `net_deny`）。
3. **SEC-K0S-002**：控制面关 OpenAPI/docs（一行）。
4. **SEC-K0S-006**：`sysinfo`/`getcpu` 已按"中介而非拒绝"处理完；`statfs` 原判"在部署形态下不生效"**已更正并修好**（真根因是 fork 的 handler 优先级，见 §SEC-K0S-007 的更正段），E2B 验收用例已摘掉 `xfail` 并转绿。
5. ~~**SEC-K0S-007（优先级高于 4）**：route-B 载荷不产生 seccomp 通知 ⇒ notif 类中介整体失效；先按文末那条修法让 exec 子进程落在 generation init 的过滤器下。~~ **同日复核实测后撤回**：route-B 载荷在过滤器下，也确实在产生通知；见 §SEC-K0S-007 的更正段。
5. `clone3` 命名空间位在 `handle_fork` 里补齐校验（纵深，当前由外层 profile 兜着）。

---

## 修复状态（2026-10-01 响应"修复所有漏洞"）

| 项 | 状态 | 改动 |
|---|---|---|
| SEC-K0S-005 | **已修复（代码）** | `envd_service/http/auth.py` 与 `envd_service/connect/router.py` 的令牌判定去掉 `allow_public_traffic` 短路：envd 控制面（`/files`、`/health`、`/envs`、Connect RPC）**一律要令牌**。`allowPublicTraffic` 仍在校验/回显（wire 兼容），但不再影响认证。回归：`tests/contract/test_network_api.py::test_network_allow_public_traffic_does_not_skip_token`（免令牌/错令牌 401、正确令牌 200，公开与默认两种沙箱各测一遍）。 |
| SEC-K0S-002 | **已修复（代码）** | `control_plane/app.py` 与 `envd_service/gateway.py` 的 `FastAPI(...)` 补 `docs_url=None, redoc_url=None, openapi_url=None`，与 `c3_agent/app.py` 对齐。回归：`tests/unit/test_control_plane_no_interactive_surface.py`（四个文档路由 404，且 `/`、`/healthz`、`/sandboxes` 行为不变）。 |
| SEC-K0S-004（IP/CIDR 形态） | **已修复（代码）** | `gateway_common/network.py::sandlock_network_policy` 的 allowlist 分支现在也用 `private_deny_cidrs`（= `E2B_NETWORK_DENY_CIDRS`，**与隐式分支同一把尺子**）做减法，deny 优先；判定用**重叠**而非包含（`0.0.0.0/0`、`::/0` 这类比保护段更宽的条目同样被丢，fail closed），并按协议族分别比较。同时修好 `_to_net_allow` 的条目解析——旧的 `entry.split(":", 1)[0]` 让 `tcp://…`、`host:port`、`CIDR:port` 三种写法**绕过**了 deny 减法。回归：`tests/unit/test_network_config.py::test_policy_explicit_allowout_is_filtered_by_private_entries`。受影响的本地源站用例改用仓库既有的 `198.18.x.y` 基准段（`tests/contract/test_mcp_netns.py` 的 `origin_alias`）。 |
| SEC-K0S-004（**域名**形态） | **未修复 —— 需要 fork 侧改动** | 域名由 fork 在建连时解析，平台侧只做字面量判定 ⇒ `allow_out=["10.244.140.26.nip.io:49983"]` 这类**解析到内网的域名**仍然可达（实测过）。正确修法是让 fork 支持"allowlist + 硬拒集并存"（deny 优先）：`NetworkPolicy::AllowList` 增一个 denied 集合、`sandbox/builder.rs` 放开 `net_allow`/`net_deny` 互斥、E2B 侧把 `net_deny = protected` 与 `net_allow` 一起下发。改动落在 `third_party/sandlock`（子模块）+ 重新出轮子 + 重出镜像 + 滚集群，**本轮未做**。 |
| SEC-K0S-006（`sysinfo`/`getcpu`） | **已修复（fork，见文末）** | fork 按"中介而非拒绝"合成：`sysinfo` 回沙箱预算、其余宿主态归零，`getcpu` 回 0。 |
| SEC-K0S-006（`statfs`） | **已修复（2026-10-01 更正）** | 语义按用户裁定"配额 + 剩余"落地；原判"部署形态下不生效、根因是 SEC-K0S-007"**作废** —— 真根因是 fork 的 **handler 优先级**（chroot 的 `SYS_statfs` handler 先注册、先答），已把 disk-stats handler 的注册前移到 chroot 之前，并加回归 `test_procfs::test_statfs_accounting_wins_over_the_chroot_handler`。E2B 验收：`tests/security/escape/test_disk_stats_statfs.py` 摘掉 `xfail` 后在 route-B 形状转绿。 |
| `clone3` 命名空间位 | **源码里已修** | 见文末更正。 |
| ~~**SEC-K0S-007**~~ | ~~高~~ **已撤回（2026-10-01 同日复核）** | 原判"route-B 形态下载荷不产生任何 seccomp 通知 ⇒ 所有 notif 类中介失效"**不成立**：同一形状（`route_b_sandbox(None, None)` + route B）里载荷的 `statfs`/`openat` 通知都到监督器（trace `notif nr=137 pid=<载荷> -> return-value`），`uname` 主机名虚拟化、`/proc` 合成、`inotify_add_watch` 中介也都在载荷上生效。`statfs` 失效的真根因是 handler 优先级，已修并钉住。详见文末更正段。 |

### fork 侧（2026-10-01 第二轮，已做完）

| 项 | 状态 | 改动 |
|---|---|---|
| SEC-K0S-004（域名形态） | **已修复（fork + E2B）** | fork 新增 **deny 优先**组合：`NetworkPolicy::AllowList` 增 `denied: DeniedDestinations`（`seccomp/notif.rs`），`allows()` 先查 deny 再查 allow；`sandbox/builder.rs` 放开 `net_allow`/`net_deny` 互斥；`sandbox.rs` 在两者并存时把解析后的 deny 集挂到同一协议的策略上（`network/rules.rs::denied_filter_from`）。E2B 侧 `sandlock_network_policy` 把 `private_deny_cidrs` **同时**下发为 `net_deny`，于是"域名解析到内网"也被拒。回归：fork 侧 `notif.rs` 新增 3 条组合单测（lib **917 passed / 0 failed**，基线 914）、`sandbox/tests.rs` 的互斥用例改为"可并存"；E2B 侧 `tests/security/escape/test_network_deny_bypass.py::test_explicit_allowout_cannot_reach_a_protected_range`（**字面量 + 主机名两种写法都必须 DENIED**，在 lane 里 **1 passed**）。 |
| SEC-K0S-006（`sysinfo`/`getcpu`） | **已修复（fork）** | fork 新增 `procfs::handle_sysinfo` / `handle_getcpu`（沿用 `handle_uname` 那套"算完写回子进程内存"的原语 `write_child_mem`）：`sysinfo` 报**沙箱自己的内存预算**（与 `generate_meminfo` 同源，也即 `/proc/meminfo` 的那份值），`loads`/`procs`/`uptime`/swap 等平台不建模的量一律**归零而不是报宿主的**；`getcpu` 两个指针都写 0。注册条件与既有虚拟化对齐（`sysinfo` 挂 `memory_limit`，`getcpu` 挂 `virtual_cpu_count`），并在 `seccomp_plan` 里把两个号加进通知集。回归：fork `test_procfs.rs::test_sysinfo_virtualization`（真沙箱里 `sysinfo` 必须回 `256 MiB / load 0 / procs 0 / uptime 0`、`getcpu` 必须回 `cpu 0`）；`procfs` 一族 **12 passed**。E2B 侧无需改动（它只是消费 fork）。 |
| SEC-K0S-006（`statfs`） | **已修复（2026-10-01）** | 语义按用户裁定："报沙箱的**配额和剩余空间**"。实现：fork 新增 `disk_stats_path` 选项（宿主维护 `<total_bytes> <used_bytes>`，handler 每次 `statfs` 现读合成 4 KiB 块、`f_type`/`f_namelen` 保留真值、文件缺失退内核）+ `SYS_statfs` 进通知集 + supervise 的 wire 字段/apply/示例；E2B 侧共享路径 helper（放在沙箱树之外，不可伪造）→ factory → executor → **agent 建时写 `total=disk_mb, used=0`、每轮磁盘扫描后刷新**。原判"部署形态下不生效、根因是通知层"**已更正**：真正的原因是 **handler 优先级** —— `build_dispatch_table` 先 `register_chroot_handlers`（注册 `SYS_statfs → handle_chroot_statfs`）再注册 disk-stats handler，而链在第一个非 `Continue` 处停止，于是每个带 chroot 根的形态（pure/合成根、镜像 rootfs、真根＝全部生产形态）都由 chroot handler 作答。修法：把 disk-stats 的注册前移；回归 `test_procfs::test_statfs_accounting_wins_over_the_chroot_handler`（修前 `4096 72335360 …`＝宿主 XFS，修后 `4096 2621440 1572864`＝账本），E2B 侧 `tests/security/escape/test_disk_stats_statfs.py` 由 `xfail` 转正向通过。 |
| `clone3` 命名空间位 | **无需修（源码里已修）** | 更正上一轮的记述：当前 fork 源码 `resource.rs::handle_fork` 的命名空间检查**已经不限于 `SYS_clone`**（用 `clone_flags` 读 `clone_args`，对整族生效），注释里记着 2026-09-30 那次"外层 profile 在替沙箱兜底"的教训。实测与之一致：`clone3` 带 `NEWUSER/NEWNS/NEWPID/...` 全部 EPERM，裸 `clone3`/`clone` 正常。 |

---

## SEC-K0S-007（**已撤回**）route-B 载荷不产生 seccomp 通知 ⇒ notif 类中介整体失效

> **2026-10-01 同日复核：结论不成立，已撤回。** `statfs` 另有真根因（fork 的 handler 优先级），已在 `third_party/sandlock` 修掉并加了红/绿回归。下面先给更正后的实测与根因，原判原文留作对照。

### 更正后的实测（本地测试镜像 `e2b-sandlock-test:latest`；形状＝`tests/security/conftest.py::route_b_sandbox(None, None)` ＋ route B，即 `statfs` 用例自己用的那个形状）

| # | 观测 | 结果 |
|---|---|---|
| 1 | 监督器 trace（`SANLOCK_EVENT_TRACE=1`，`RouteBInstance.slot_stderr()` 取回） | 载荷**自己**的 syscall 在矩阵里：`notif nr=257/262/3/12/9/11 pid=<载荷>`，以及 **`notif nr=137 pid=<载荷> -> return-value`**（amd64 镜像；arm64 上 `statfs` 是 43） |
| 2 | 载荷 `os.uname().nodename` | `sandbox-11-1`（虚拟名），宿主是 `orbstack` ⇒ `handle_uname` 在载荷上生效，也就是 `write_child_mem` 这条路可用 |
| 3 | 载荷读 `/proc/meminfo` | `MemTotal: 524288 kB`（＝沙箱预算），宿主 `20554504 kB` ⇒ 中介合成生效（全仓只有 `procfs.rs` 产出这段文本，且只从 notif handler 可达） |
| 4 | 同形状的 inotify 中介用例 | `test_path_surface_inotify.py::test_the_pure_shape_is_mediated_too_and_the_watch_stays_inside` **通过**（宿主目录被拒 —— Landlock 没有 inotify 权限位，只能是 notif 中介） |
| 5 | 同一次跑的两条用例 | `1 passed, 1 xfailed`（后者即 `statfs`） |
| 6 | 生产侧旁证（本报告 SEC-K0S-006） | 载荷读到中介合成的 `/proc/meminfo`、`/proc/mounts`（"sandlock" 设备名）⇒ 线上同样在通 |

**原判两个依据为什么站不住**：

* "载荷自报的 pid 一条都没有"：载荷报的是 **pid 命名空间里的 pid**（生产 `E2B_PID_NS=true`），矩阵里的 pid 是监督器命名空间（容器）的 pid，两者本来就不可比；
* "`nr=137` 全程未出现"：`12/257/262/302/137` 是 **x86_64** 号（本地 amd64 镜像如此），而 k0s 车队是 arm64（`statfs`＝43、`openat`＝56、`newfstatat`＝79、`prlimit64`＝261）—— 拿 x86_64 号去 arm64 的矩阵里找 `statfs` 本身不成立；
* 更要紧的是自相矛盾：SEC-K0S-006 那张表（载荷读到中介合成的 `/proc`）与"载荷收不到通知"不可能同时为真。

### 真根因：`statfs` 的 handler 被 chroot 路径 handler 抢答（已修）

`crates/sandlock-core/src/seccomp/dispatch.rs::build_dispatch_table` 的注册顺序是「chroot 在前、disk-stats 在后」（第 600 行调 `register_chroot_handlers`，第 687 行才注册 disk-stats），而 `DispatchTable::dispatch` 的规则是**第一个非 `Continue` 的结果生效**，`handle_chroot_statfs` 又总是作答（真实数字或 errno）。于是**每个带 chroot 根的形态**（pure/合成根、镜像 rootfs、真根 —— 即全部生产形态）里 `disk_stats_path` 都轮不到执行；fork 自己的集成用例不带 chroot，所以一直是绿的（"每段都成立、合起来不生效"的真正原因）。

最小复现（同一 wheel、同一策略，只差 `chroot`）：

```
no-chroot : (4096, 4096, 2621440, 1572864)   ← 账本 10 GiB / 已用 4 GiB
chroot=/  : (4096, 4096, 72335360, 52400913) ← 宿主 XFS，accounting handler 被抢
```

route-B 形状里还有一条只读判别：把账本路径换成**命名管道**，修复前载荷的 `statfs` 立刻返回宿主数字（说明 disk-stats handler 从未被调用），修复后会卡在管道读上（说明它确实被调用了）。

**修法（已落）**：把 disk-stats 的注册移到 `register_chroot_handlers` 之前。`handle_statfs` 本来就与路径无关（账本是"卖了多少 / 用了多少"），所以它就是"配置了账本时所有 `statfs`"的正确答案。

**回归与验收**：

* fork：新增 `test_procfs::test_statfs_accounting_wins_over_the_chroot_handler`（`chroot("/")` ＋ 账本）—— 修前 `4096 72335360 …` 红，修后 `4096 2621440 1572864` 绿；同批 `test_procfs` 14 passed、`test_chroot` 52 passed、`--lib dispatch` 21 passed。
* E2B：`tests/security/escape/test_disk_stats_statfs.py` 摘掉 `xfail(strict=True)` 后在 route-B 形状转绿（`df` 读到账本，改写账本后下一次调用即生效），同形状的 inotify 用例仍绿；`tests/security/escape` ＋ `tests/unit/test_route_b_wiring.py` 合计 43 passed。
* 轮子/镜像按修复重建（`build-sandlock-wheels.sh` 两个架构 + `build-test-image.sh`）。

### 原判（已撤回，保留对照）

**这是为 SEC-K0S-006 的 `statfs` ver证时顺带凿出来的，比 006 本身严重。**

#### 事实（原判，全部运行时证据；诊断代码已从 fork 清除）

在为 `statfs` 做中介时，链路的每一段各自都"成立"，但部署形态下就是不生效。逐段仪表化后得到：

| # | 观测 | 结果 |
|---|---|---|
| 1 | 被 confine 子进程的计划（在沙箱内落盘） | `CTX notif branch fs_writable=3 disk_stats=Some("…/disk-stats") statfs=true` —— 过滤器**含** `SYS_statfs` |
| 2 | 监督器注册 handler（宿主 `/tmp` 落盘） | `DISPATCH statfs handler registered path="…/disk-stats"` |
| 3 | `handle_statfs` **入口**日志 | **从未触发** |
| 4 | 载荷里裸调 `syscall(137)`（以及 `195` `statfs64`、`138` `fstatfs`） | `rc 0`，返回**宿主**块数（`72564736` 量级）⇒ 内核原样执行了 |
| 5 | 监督器**派发点**的 per-(pid, syscall) 通知矩阵 | pid 60/62/65/68/80 上报了 `brk(12)`/`openat(257)`/`close(3,59)`/`newfstatat(262)`/`prlimit64(302)`/`access(21)`/`mmap(9)`/`fstat(47)`…；**载荷自报的 pid（87）一条都没有**；`nr=137` 全程未出现 |

载荷跑着 CPython（第 5 项说明它必然发 `openat`/`close`），却**一条通知都没有** ⇒ 它根本不在那层过滤器下。载荷里的 `Seccomp: 2` 来自 pod 自身的 seccomp profile，不是 sandlock 那层（`prctl(PR_GET_SECCOMP)` 只给"有没有过滤器"，给不出来源）。

> 更正：第 3 项成立（`handle_statfs` 确实没被调用），但它说明的是**handler 被抢答**，不是"载荷不在过滤器下"；第 4/5 项是 arch 号与 pid 命名空间的误读，见上节。

#### 影响（原判，已作废）

* ~~**所有基于 seccomp-notify 的中介，对 route-B 形态的载荷都不生效** —— 已知消费方包括 `statfs`（本轮）、`uname` 主机名虚拟化、`sched_getaffinity`/`num_cpus`、`/proc` 合成的那些路径、以及依赖 notif 的网络/路径中介。~~
* ~~该形态当前的隔离实际来自 **mount namespace/真根、uid 池、Landlock、pod 自身 profile**，而不是 sandlock 的 notif 层 —— 这是个**需要单独决策**的口径问题。~~
* 作废理由：上节第 2/3/4 项实测都在载荷上生效；该形态的 notif 层是活的。

#### 修法方向（原判，已作废）

~~让 route-B 的 exec 子进程落在 **generation init 的过滤器**下：要么确保子进程由那个被 confine 的 init fork（`RunExec` 链路），要么在 fork 后按 generation 的 plan 重新 confine 一次。~~

作废理由：exec 子进程本来就是被 confine 的 init fork 出来的（`RunExec` → init fork → `execve`），照这条改不会修好 `statfs`。真正要改的是 handler 注册顺序。
| 轮子/镜像 | **已重建** | `wheels/fork/` 两个架构（x86_64 + aarch64）都重新出了（`build-sandlock-wheels.sh`，zig 交叉编译，无 QEMU）；本地 `e2b-sandlock-test:latest` 已按新轮子重建（`build-test-image.sh`，这个镜像必须随之重建，否则仍是旧行为 —— 见 `build-test-deploy-pitfalls.md` §B7）。旧轮子里那句 `--net-allow and --net-deny are mutually exclusive` 已从 `.so` 中消失（唯一性检查仍在），可据此确认轮子确实带上了这次改动。 |

**尚未部署**：以上代码改动只落在仓库里；`0.1.0-824-gf2aec0b-20261001-073534` 的集群上仍是修复前的行为。
上线需要按 `deploy-clusters.md` 走 `build-and-push.sh` + `apply.sh`（并先建通道、自检集群身份）。

**工具链注记（给下一个接手的人）**：本机 macOS 编译不了 fork（Linux-only API，`cargo check` 671 条错误），fork 的验证只能在容器里跑。
可用的最小回路（比 `fork-gate.sh` 全档快得多）：

```bash
docker run --privileged --rm --entrypoint sh -v "$PWD":/src -w /src/third_party/sandlock \
  sandlock-dev-f17:latest -c '
  chmod a+rx /root; chmod -R a+rX /root/.cargo /root/.rustup
  chmod -R a+rwX /src/third_party/sandlock/target-linux
  mkdir -p tmp/cargo-home tmp/home; chmod -R a+rwX tmp/cargo-home tmp/home
  setpriv --reuid 65534 --regid 65534 --clear-groups env \
    CARGO_HOME=/src/third_party/sandlock/tmp/cargo-home HOME=/src/third_party/sandlock/tmp/home \
    CARGO_TARGET_DIR=/src/third_party/sandlock/target-linux PATH=/root/.cargo/bin:/usr/bin:/bin \
    cargo test -p sandlock-core --lib'
```

**注意**：不要给这个容器加 `--offline`（镜像用的是 rsproxy sparse registry，没有 vendored 源）；`CARGO_HOME` 指到 fork 的 `tmp/cargo-home`（可写）而不要用 `/root/.cargo`（65534 写不了）。
另外记一条与本轮改动无关的观察：`test_egress` 那套夹具里"**被策略拒绝**的 connect"会让 `policy.run` 不返回（allow-only 和 allow+deny 都复现，属既有行为，不是本次引入），所以我没有把那条集成用例留在库里。
