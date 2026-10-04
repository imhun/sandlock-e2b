# k0s 线上沙箱白帽审计（第三轮）—— 发现与结论（2026-10-04）

> **状态：SEC-R3-01、STATIC-2、STATIC-5 已修复（仅工作区，未构建/未上集群）。**
> STATIC-1 / STATIC-3 / STATIC-4 / STATIC-6 未改动。
> 修复的实施记录与验证见 `docs/security-audit/remediation-SEC-R3-01.md` §8。
> 「哪些 syscall 只靠外层 profile 挡住」的**实测**归因见
> `docs/security-audit/layer-attribution-2026-10-04.md`。

目标：**沙箱逃逸 → 突破 worker 容器 → 触达物理节点**。

本文件的 §1–§6 是**探测结论**（证据与判据）；修复的实施记录在
`remediation-SEC-R3-01.md` §8。未列入修复的条目保持原样。

- 目标形态（现场核对，非文档转述）
- 结论摘要
- SEC-R3-01（Critical）`secure` 由客户端控制 ⇒ envd 令牌校验失效 ⇒ 经网关零凭据远程命令执行
- 本轮推翻/判定已修复的旧结论（Important — 不要重复劳动）
- 本轮实测判定干净的面
- 静态审计遗留（未逐条现场复现）
- 架构安全评估
- 复现方式

---

## 0. 目标形态（2026-10-04 现场核对）

| 项 | 实测值 |
|---|---|
| 集群 | 自建 k0s `v1.36.4+k0s`，2 节点，namespace `sandlock` |
| 节点 | Rocky Linux 10.2，**kernel `6.12.0-211.34.1.el10_2.aarch64`**，containerd 2.3.4，CNI = Calico |
| 节点角色 | `izuf697v12g31dyz4uvsjlz` = control-plane **且** 跑 `e2b-worker-0`（**控制面与工作节点同机**） |
| worker | 镜像 `e2b-sandlock-worker:0.1.0-975-g26eba34-20261003-215907` |
| worker 权限 | `runAsUser/Group 65534`、`capabilities.drop:[ALL]`、`seccompProfile: Localhost sandlock-worker.json` |
| 沙箱形态 | **`E2B_REAL_ROOT=1`**（真根 pivot_root）、`E2B_PID_NS=true`、`E2B_ENABLE_NET_ISOLATION=true`、`E2B_FD_INJECT_CONNECT=true` |
| worker hostPath | `/var/lib/e2b/workspaces`、`/var/lib/e2b-images`、`/var/lib/e2b/state` |
| c3-agent | `hostPID: true`；face A `uid 65534 + SETUID/SETGID`；**face B `uid 0 + CHOWN/DAC_OVERRIDE/FOWNER`** + 同样三个 hostPath |
| NetworkPolicy | **全 namespace 只有 1 条**：`e2b-c3-agent`（ingress 仅 `app=control-plane`，49985/49986）。**没有 default-deny** |
| 入口 | `172.18.78.49:3000` = CP + gateway（同进程），对外可达 |

沙箱内实测身份：`uid=0(root) gid=0(root) groups=0(root),65534`，`/proc/net/dev` **只有 `lo`**（per-sandbox netns）。

---

## 1. 结论摘要

**内核/容器隔离层是这套系统里最硬的部分，本轮没能打穿。** 逃逸尝试全部落在控制面。

本轮找到 **1 个 Critical，且已端到端实测成立**：控制面把 `secure` 当成客户端参数，
导致 envd 的访问令牌校验退化为恒真；而对外入口（gateway）**自身完全不做认证、也不检查
`allowPublicTraffic`**，于是"发布一个公开沙箱"这个正常需求可以退化成**互联网可达的零凭据 RCE**。

一句话版本：

> **沙箱 → 节点：不通（内核层 + 网络层双重挡住）。**
> **沙箱/外部 → 控制面 → 任意沙箱：通（Critical）。**

**第二轮（复现静态遗留）没有发现新的可利用逃逸，但把 6 条静态结论全部落定**，
其中三条的定级与上一版不同：

- **STATIC-2**（7 个 `*at` syscall）：确证只靠外层 profile 挡，但**内核 6.12 根本没实现
  它们**（7/7 `ENOSYS`）⇒ 今天零内核攻击面。**已做升级预演**（§5.2.1，本机 7.0 内核）：
  外层一旦放宽，`fchmodat2` **立刻执行成功并真的改掉文件 mode**；
  而 **Docker/OCI 内置默认 profile 就不挡它** —— 这道防线是"漏了就默认拒"顺手挡住的，
  profile 里 7 个名字一个都没出现。
- **STATIC-3**（`CLONE_NEWNET` 漏出 `CLONE_NS_FLAGS`）：**从 Med 降到 Low**。漏项是真的，
  但外层 profile 的 clone 掩码（`0x7E020000`）**含 bit30、比内层更完整**，且 `capget` 实测
  `CAP_SYS_ADMIN` 已被清除 ⇒ 还有内核兜底。**但纠正一条连带错误**：外层的
  `clone3 → ENOSYS` 规则**实测未触发**（clone3 真的到了内核，返回 `EINVAL`），
  所以 clone3 路径上唯一的禁令执行点是 `resource.rs:114-116` 那个 TOCTOU 检查 ——
  今天安全**完全依赖 CAP_SYS_ADMIN 被清除**。
- **STATIC-5**（c3-agent）：**本地编译真实特权二进制实证** `--worker` 确实绕过 pool 门禁、
  `walk` 确实能枚举整个 root；同时实证**穿越与符号链接逃逸 5/5 全部被拒**。

新增两条值得记住的事实：**沙箱的 bounding set 是满的**（安全性依赖 permitted 为空 +
`NO_NEW_PRIVS`，不是依赖 bounding set）；**全舰队唯一的 uid 0 容器（c3-agent `maint`）
没有任何 seccomp 约束**，而 worker 有。

---

## 2. SEC-R3-01（**Critical**，已实测端到端成立；✅ Phase 1 已修复，见 remediation §8.1）

### 2.1 根因（四个环节，全部由客户端输入决定，无一校验）

```
control_plane/api/sandboxes.py:1103      secure = body.get("secure", True)          # 客户端可控
control_plane/registry/manager.py:1630   envd_access_token = access_token() if secure else ""
envd_service/connect/router.py:67        if runtime.access_token and token != runtime.access_token:
envd_service/http/auth.py:68             （HTTP 半边同一条守卫）
```

`secure=false` ⇒ `envd_access_token = ""` ⇒ 守卫左项为空 ⇒ **恒真** ⇒
`E2b-Sandbox-Id`（同样由客户端指定）就是全部凭证。

`secure` 不是隐藏参数：SDK 里它是公开且有文档的参数
（`.venv/.../e2b/sandbox_sync/main.py:171` `secure: bool = True`，
docstring 明写 *"Envd is secured with access token and cannot be used without it, defaults to True"*）。
也就是说**官方 SDK 直接支持把它关掉**。

### 2.2 为什么能打到集群外面：gateway 自己不认证

`envd_service/gateway.py:277+` 的 `proxy()`：

```python
sandbox_id = request.headers.get("E2b-Sandbox-Id")
if not sandbox_id:
    return Response(status_code=401, content="Missing E2b-Sandbox-Id header")
address = await _resolve_address(sandbox_id)   # 用 gateway 自己的 internal key 查
... _forward(address, path, request, body)     # 原样转发
```

- 入口**没有任何认证**（无 API key、无 token）。
- `_resolve_address` 用 **gateway 自己持有的 fleet internal key** 去问控制面要地址 —— 等于把控制面的
  路由能力借给了任何能连到入口的人。
- 后端路由端点 `control_plane/api/internal.py:1112-1124` 只检查三件事：
  沙箱存在 / 节点存在 / 节点 healthy。**完全不检查 `allowPublicTraffic`**（已核对源码 + 现场）。

⇒ **对任意已存在的 sandbox_id，网关都会把它送到那台 worker 的 envd。**
envd 的令牌是唯一剩下的闸门，而 `secure:false` 把它拆了。

### 2.3 实测证据（证据矩阵）

探针：`tmp/audit3/s5_rce_proof.py`（集群内）+ `tmp/audit3/s6_escalation.py`、
入口侧 curl 见 §6。原始读数 `tmp/audit3/out-rce-proof.json`。

**(A) 直连 worker envd（集群内，从 CP pod 发起）**

| 用例 | 沙箱 | 令牌 | 结果 |
|---|---|---|---|
| A | `secure=true` | 无 | `unauthenticated: Invalid access token` ✔ 闸门有效 |
| B | `secure=true` | 错 | `unauthenticated: Invalid access token` ✔ |
| **C** | **`secure=false`** | **无** | **执行成功** ✘ `uid=0(root) gid=0(root) groups=0(root),65534` / `sandbox-76-1` / `PROOF-42` / `exitCode 0` |
| D | `secure=false` 打到**另一台** worker | 无 | 拒绝：`belongs to node e2b-worker-0, not e2b-worker-1` ✔ 槽位归属有效 |
| E | 不存在的 sandbox_id | 无 | `unauthenticated: Sandbox ... not found` ✔ 注册表查找仍有效 |

**(B) 经对外入口 `172.18.78.49:3000`，零凭据（连 `X-API-Key` 都没有）**

| 场景 | 结果 |
|---|---|
| `secure=false` 沙箱，`POST /process.Process/Start` | **HTTP 200 + 执行成功**：`uid=0(root)` / `sandbox-594-1` / `GATEWAY-RCE-77` / `cat /etc/hostname` = `debuerreotype` / `exitCode 0` |
| `secure=true` 沙箱，同样零凭据 | `unauthenticated: Invalid access token` ✔ |
| **`secure=false` + `allowPublicTraffic:true`**（现实组合） | **HTTP 200 + 执行成功**：`uid=0(root)` / `PUBLIC-UNAUTH-RCE-999` / `exitCode 0` |

最后一行是关键：**`allowPublicTraffic` 的用途就是让沙箱对外可见，因此它的 `sandboxID`
按设计就是公开信息** —— 于是 id 不再需要"猜"，`2^64` 的 id 空间这层保护也没了。

### 2.4 完整攻击链

```
外部攻击者
  └─ POST http://<入口>:3000/process.Process/Start
       Content-Type: application/connect+json
       E2b-Sandbox-Id: sbx_<公开沙箱 id>          ← 无任何凭据
          │
          ├─ gateway.proxy()  无认证，放行
          ├─ GET /internal/routes/{id}（用 gateway 自己的 internal key）
          │      不检查 allowPublicTraffic → 返回 http://10.244.140.6:49983
          └─ envd process.Process/Start
                 runtime.access_token == ""  →  守卫恒真
                    ⇒ 任意命令以 uid 0 在该沙箱内执行
```

### 2.5 影响

- **凭据完全丢失**：一个正常发布出去的公开沙箱 = 一个挂在公网上的 root shell。
- **横向**：拿到任一 `secure:false` 沙箱的 id 就能完全控制它（文件 API 全线同样失效，
  `envd_service/http/auth.py:68` 是同一条守卫）。`/files` 上传下载同样无需凭据。
- **对节点的影响**：见 §4 —— **这一条本身不等于节点逃逸**，因为拿到的仍是沙箱内 uid 0，
  依然受 pivot_root + Landlock + seccomp blocklist + `drop ALL` 四重约束（§3 已实测）。
  但它把"应用层漏洞"变成了"可自由使用的执行原语"，任何一处内核/中介偏差都会被它放大。

### 2.6 修法建议（本轮不实施）

1. `secure` **不要从请求体取**。它要么删掉，要么只允许服务端策略决定；若必须保留，
   至少在 `secure=false` 时强制 `allowPublicTraffic=true`，让"关掉令牌"和"公开"两个
   危险开关不能被一次请求同时按下。
2. 守卫改成**空令牌即拒绝**（fail-closed），而不是"空令牌即放行"：
   `if not runtime.access_token or token != runtime.access_token: refuse`。
   这一处改动同时修掉 HTTP 与 connect 两个半边。
3. gateway 入口加认证（至少要求有效 API key 或 traffic token），并让
   `/internal/routes/{sandbox_id}` 真正检查 `allowPublicTraffic`。
4. 加一条不变量测试：**任意 `secure=false` 沙箱，envd 必须在无 token 时拒绝一切请求**。

---

## 3. 本轮**推翻**或判定**已修复**的旧结论（重要，避免下轮重复劳动）

### 3.1 S-1「工作负载继承了 seccomp notify fd，可自行应答通知」——**推翻，不成立**

静态分析给出了 Critical 级推论：`context.rs:997-1022` 用
`SECCOMP_FILTER_FLAG_NEW_LISTENER` 拿到 notify fd，`keep_fd` 进入 `close_fds_above` 的
保留清单，而内核不给这个 fd 设 `FD_CLOEXEC` ⇒ 负载 exec 后仍持有它 ⇒ 可以自己
`SECCOMP_IOCTL_NOTIF_SEND{CONTINUE}` 抢先应答自己触发的通知，绕过整个用户态中介层。

**现场实测（`tmp/audit3/s1_notify_fd.py`）**：遍历 fd 0–127，对每个 fd 做
`F_GETFD` + `SECCOMP_IOCTL_NOTIF_ID_VALID`（`0x2100`）：

```
fd 0/1/2/3 = pipe（stdin/stdout/stderr + 1）
NOTIF_ID_VALID_errno = "Inappropriate ioctl for device"（ENOTTY）全部
is_seccomp_listener = false
=> 结论：no seccomp listener fd in the workload
```

**只有 fd 0–3，全是管道，没有任何 seccomp listener。** 该推论在生产形态不成立。
（`realroot.rs:138-141` 给 trace fd 显式 `fcntl` 设过 `FD_CLOEXEC`，notify fd 这条路径
在生产形态被别的机制挡住了 —— 但**代码注释里的推理链仍然是错的**，见 §5-STATIC-1。）

### 3.2 SEC-K0S-004「`allowOut` 无私网/集群网段过滤」——**已修复**

上一轮（2026-10-01）实测 `allow_out` 里的内网 IP 可达。当前部署：

| `allowOut` | 目标 | 结果 |
|---|---|---|
| `151.101.0.223`（公网 IP） | :443 | **CONNECTED**（证明 allowOut 本身有效） |
| `10.103.167.2`（ClusterIP） | :3000 | `111:Connection refused` |
| `10.244.140.6` / `10.244.192.252`（worker envd） | :49983 | refused |
| `10.98.99.192`（redis） | :6379 | refused |
| `10.244.140.32`（**c3-agent**） | :49985 / :49986 | refused |
| `192.168.1.1` | :80 | refused |
| `169.254.169.254`（云 metadata） | :80 | refused |

**私网、集群网段、link-local 即使被显式写进 `allowOut` 也一律拒绝**，而公网 IP 正常放行。
⇒ 沙箱 → 内网的横向腿已被网络层切断。**上一轮的 SEC-K0S-004 请标记为已修复。**

### 3.3 SEC-K0S-005（`allowPublicTraffic` 免 envd 令牌）——**已修复**（代码层）

`envd_service/http/auth.py:56-68` 与 `envd_service/connect/router.py:60-68` 现在都**无条件**要求
`X-Access-Token`，并留了明确的注释禁止再把 `allow_public_traffic` 放回这个条件。
实测 `secure=true` 沙箱无论直连还是经网关，无 token 一律 `Invalid access token` ✔。

> ⚠️ 但请注意：**SEC-R3-01 从另一条路把同一个洞打开了**（`secure=false` 而不是
> `allowPublicTraffic`）。修 005 的人没有注意到 `secure` 也是客户端可控的。

---

## 4. 本轮实测判定**干净**的面（省下后续重复劳动）

| 面 | 判据（现场） |
|---|---|
| **沙箱 → c3-agent（节点特权面）** | 从 **worker pod 内**直连 `10.244.140.32:49985/49986` → **无响应（被 Calico DROP）**；从沙箱内同样 refused。**唯一那条 NetworkPolicy 生效，fail-closed。** |
| **沙箱 → 控制面 / redis / gateway** | 沙箱内全部 refused（§3.2）；但 **worker pod 内可达**（`10.103.167.2:3000` CONNECTED、`10.98.99.192:6379` CONNECTED、`127.0.0.1:49983` CONNECTED）。控制面/redis **没有任何 NetworkPolicy**。 |
| **内核隔离四重锁** | `capabilities.drop:[ALL]` + worker `Localhost` seccomp profile + 内核 seccomp blocklist（`mount/ptrace/bpf/io_uring/keyctl/open_tree/statmount/open_by_handle_at/…` 全 EPERM）+ Landlock（fail-closed）+ `E2B_REAL_ROOT=1` pivot_root。**全部未能绕过。** |
| **`/proc` 姿态** | `/proc/self/{stat,status,cmdline,exe,maps,fd,root,environ,limits,ns/*}`、`/proc/1/{stat,root}`、`/proc/kallsyms`、`/proc/net/tcp`、`/proc/sys/kernel/hostname` **全部 EACCES**。比代码里声明的白名单还紧 ⇒ 偏 fail-closed，无洞。 |
| **`/dev/pts` 跨租户 pty**（静态审计曾标 Medium） | 生产 real-root 形态下 `/dev/pts` **只有自己的 `ptmx`**（newinstance devpts），看不到任何邻居 pty；`openat("ptmx")` 拿到的是自己的。**该面在生产形态不存在。** |
| **槽位归属** | 把 `secure=false` 沙箱的请求打到**另一台** worker → `HTTP 403: belongs to node e2b-worker-0, not e2b-worker-1`。**跨节点路由被 CP 挡住。** |
| **sandbox_id 猜测** | `sbx_` + 16 hex（2^64），不可枚举；不存在则 `not found`。仅在 `allowPublicTraffic` 场景下按设计公开（§2.3）。 |
| **c3-agent 代码面**（静态） | 单一路由、认证先于一切、`secrets.compare_digest`、空 token 启动即拒（fail-closed）、uid 0 三重禁止、pid 需 NSpid+cgroup 双证明、`realpath` 前后缀包含检查 + `FTS_PHYSICAL|lchown`（**`..`/符号链接逃逸不可能**）、无 `shell=True`、无用户提供的二进制。 |
| **c3-agent 路径包含（本地实证）** | 用真实编译出的 `e2b-maint` 跑 5 条穿越/符号链接逃逸（`..` 出 root、`..` 回 root、`walk`+`..`、`chown` 经 symlink、`walk` 经 symlink）⇒ **5/5 全部 refused**。见 §5.5。 |
| **沙箱能力集** | `capget` = `0xffdfffff`：除 `CAP_SYS_ADMIN`（bit21，已被 `drop_cap_sys_admin()` 清除）外几乎全有；`NO_NEW_PRIVS=1`。⇒ 命名空间创建类标志在内核层被拒。**注意 bounding set 是满的**（`unshare(CLONE_NEWUSER)` 会重授），安全性依赖 permitted 为空 + NNP，而非 bounding set。 |

---

## 5. 静态审计遗留 —— **第二轮已逐条复现**

探针：`tmp/audit3/s8_static_repro.py` / `tmp/audit3/s9_caps_clone.py` / `tmp/audit3/s10_caps_only.py` / `tmp/audit3/s11_clone3_safe.py`，
外加**本地编译** c3-agent 的真实特权二进制跑 `tmp/audit3/repro_c3_maint.sh`。

| 编号 | 静态结论 | 复现结果 | 修正后的定级 |
|---|---|---|---|
| STATIC-1 | 负载继承 seccomp notify fd | **再次实测否定**（fd 0/1/2，`NOTIF_ID_VALID` 全 ENOTTY） | 见 5.1 |
| STATIC-2 | 7 个 `*at` syscall 只靠外层 profile 挡 | **确证**：内核 6.12 全部 `ENOSYS` ⇒ 今天零内核攻击面 | Low（升级型），见 5.2 |
| STATIC-3 | `CLONE_NS_FLAGS` 漏 `CLONE_NEWNET` | **确证存在**，但**另有两层仍挡住** ⇒ 潜伏非活跃 | Low，见 5.3 |
| STATIC-4 | 3 个 handler 无策略门禁 | **确证门禁缺失**，但生产形态**观察不到数据泄露** | Info，见 5.4 |
| STATIC-5 | c3-agent `--worker` 越过 pool 门禁 / `walk` 非严格 | **本地编译实证成立**；穿越与符号链接逃逸**全部被拒** | Med（条件于 token），见 5.5 |
| STATIC-6 | c3-agent pod 卫生 | **现场确证** | Low，见 5.6 |

### 5.1 STATIC-1 —— 再次否定，但注释仍是陷阱

第二次独立测量（`s8`/`s10` 各自的 fd 遍历）都是同一结果：**只有 fd 0/1/2，全是 pipe，
`SECCOMP_IOCTL_NOTIF_ID_VALID` 一律 `ENOTTY`**。

值得记的一点：`/proc/self/fd` 列举被 procfs 中介拒绝，所以这个结论**不能**靠 `ls /proc/self/fd` 得出 ——
必须逐 fd 做 `F_GETFD` + ioctl 探测。任何后续复核请照此做，否则会误判为"列举失败=没泄漏"
而其实是"列举失败=没检查"。

代码注释（`context.rs:997-1042`）的推理链仍然与实测不符，建议改注释而非改代码；
真正的边界更可能是 `real_root.rs:138-141` 那类显式 `FD_CLOEXEC`。把 notify fd 也补上
`FD_CLOEXEC`，可以让安全性从"实测干净"升级为"结构保证"。

### 5.2 STATIC-2 —— 确证，且**风险窗口可以精确定位**（✅ 已修复，见 remediation §8.3）

`sys/path_surface.rs:159-210` 的 7 个 syscall（`fchmodat2` 452、`setxattrat` 463、
`getxattrat` 464、`listxattrat` 465、`removexattrat` 466、`file_getattr` 468、
`file_setattr` 469）在 `structs.rs` 与 `seccomp_plan.rs` 里 **grep 无命中** ⇒ 内层 BPF
既不 deny 也不 notify，纯粹落到外层 profile 的 `SCMP_ACT_ERRNO` 默认动作。

**现场实测（`s8`）：七个全部返回 `ENOSYS` / "Function not implemented"。**

关键是这个 errno 的含义：`ENOSYS` 由内核在**参数校验之前**返回，所以它证明的是
**内核根本没有实现这 7 个 syscall**（6.13 才引入），而不是"被某层过滤器拒绝"。
⇒ **今天内核侧攻击面为零**，外层 profile 是第二重保险。

风险窗口因此可以写死：**节点内核升到 ≥6.13 且外层 profile 被换宽的那一天**。
届时 Landlock **不覆盖 xattr 写**（代码自己在 `path_surface.rs:274-275` 承认），
这 7 个就变成对容器内任意可遍历路径的元数据读/写原语。

### 5.2.1 升级预演：内核一旦实现这 7 个 syscall，会发生什么

线上 6.12 无法验证"内核已实现"的情形，所以用本机 Docker（内核 **7.0.14-orbstack**，
≥6.13）做反事实实验。探针 `tmp/audit3/static2_probe.c`，三种外层配置：

| 外层 seccomp | `fchmodat2`(452) | 其余 6 个 | 说明 |
|---|---|---|---|
| **仓库自带 `sandlock-worker.json`** | **ENOSYS(38)** | 全部 **ENOSYS(38)** | 防线有效 |
| `seccomp=unconfined`（外层放宽的反事实） | **执行成功，ret=0** | 5 个 EINVAL/EFAULT/E2BIG，**无一带 EPERM** | 全部**到达内核** |
| **Docker 内置默认 profile** | **执行成功，ret=0，文件 mode 真的变成 0644** | 4 个 EINVAL，`file_getattr`/`file_setattr` ENOSYS | **默认档不挡 `fchmodat2`** |

三条硬结论：

1. **只要内核实现了，内层 filter 就完全不设防。** `seccomp=unconfined` 下 7 个无一被
   seccomp 拦下（没有一个返回 `EPERM`），其中 `fchmodat2` 与 `file_getattr` **真的执行成功**。
   这与静态判读一致：`sys/structs.rs` / `seccomp_plan.rs` 里 grep 无命中 ⇒ BPF 落 `RET_ALLOW`。
2. **整个缓解措施完全依赖"恰好挂了这份 profile"。** Docker/OCI **内置默认 profile 放行
   `fchmodat2`**（实测执行并修改了文件 mode）。也就是说换成任何默认档的运行时
   （裸 `docker run`、某些 systemd 单元、自建 OCI 配置），这道防线就没了。
3. **它是被"默认拒绝"顺手挡住的，不是被显式挡住的。** 仓库 profile 里
   `fchmodat2`/`setxattrat`/… **一个名字都没出现**，全部靠
   `defaultAction: "SCMP_ACT_ERRNO"` 兜底。而同一份 profile **显式列了
   `fchmod`(107) 与 `fchmodat`(108)** —— 说明作者考虑过 chmod 类 syscall，
   `fchmodat2` 只是**默默地溜过去了**。靠"漏了就默认拒"来维持安全，
   任何一次把默认动作反转、或换运行时的 profile 重写，都会静默打开这个洞。

关于 xattr 那 4 个：本机内核上它们**确实到达内核**（`EINVAL`/`EFAULT`），
但我的参数布局未对齐（`6b AT_EMPTY_PATH` 给出 `EFAULT`、`file_setattr 6b` 给出 `E2BIG`
都说明内核在读用户指针），**因此"xattr 写落地"这一步本轮未演示成功，不作断言**。
不过影响链条的关键一环可以独立确认：**对照组里经典 `setxattr`/`lsetxattr`/`fsetxattr`
全部执行成功且 readback `PRESENT`** —— 这些在 fork 的台账里属于 `NON_PATH_SYSCALLS`
（`path_surface.rs:401-481`），本来就是放行的。加上代码自认
（`path_surface.rs:274-275`）**Landlock 不覆盖 xattr 写**，
所以"`setxattrat` 一旦存在即成为路径中介的旁路"这个判断成立，只是未实测落地。

**另需更正本报告上一版的一处错误**：上一版称 ledger 里"实测 ENOSYS"是错的、
"实际会给 EPERM"。**这个纠正是错的** —— 用仓库真实 profile 实测，
7 个全部返回 **ENOSYS(38)**，与 ledger 声称一致。机制是
`defaultAction: SCMP_ACT_ERRNO` 在该运行时下解析为 errno 38。
ledger 是"全量分类"测试（`path_surface.rs:680` `every_arch_syscall_is_classified`）
的唯一依据，它的实测描述是**准确**的。

### 5.3 STATIC-3 —— 确证存在，但**不是只有一层在挡**（原报告判断需要修正）

**源码层（无歧义）**：`sys/structs.rs:203-215` 的 `CLONE_NS_FLAGS` 逐项列出
`NEWNS|NEWCGROUP|NEWUTS|NEWIPC|NEWUSER|NEWPID`，**确实没有 `CLONE_NEWNET` (0x4000_0000)**。

**现场层（`s10`，这才是定级的依据）**：`capget` 成功，读数为

```
effective = permitted = 0xffdfffff
```

即沙箱持有**几乎全部能力**（`CAP_SYS_PTRACE`、`CAP_NET_ADMIN`、`CAP_SYS_MODULE`、
`CAP_SYS_RAWIO`、`CAP_MKNOD`、`CAP_DAC_OVERRIDE`、`CAP_SYS_CHROOT`、`CAP_SETFCAP`、
`CAP_AUDIT_CONTROL` …），**唯独 bit21 `CAP_SYS_ADMIN` 被清除** —— 这正是
`realroot.rs` 的 `drop_cap_sys_admin()`。另外 `PR_GET_NO_NEW_PRIVS = 1`。
bounding set 仍是满的（`unshare(CLONE_NEWUSER)` 会重授满 bounding set），但
permitted 为空 + NNP ⇒ exec 无法再捞回任何能力。

⇒ **命名空间标志需要"当前 userns 内的 CAP_SYS_ADMIN"，而它没有 ⇒ 内核层面 EPERM。**
所以 STATIC-3 是**潜伏**，不是活跃。

**外层 profile 其实比内层更完整**（此前未核实的关键一点）：
`deploy/seccomp/sandlock-worker.json` 的 `clone` 规则掩码是
`2114060288 = 0x7E020000`，**bit30 = `CLONE_NEWNET` 在内**。
所以 `clone(CLONE_NEWNET)` 被**外层**挡住，内层的漏项被掩盖。
（该规则的 `includes: {}` 表示适用所有架构，`excludes: {caps:[CAP_SYS_ADMIN]}`
在 `drop ALL` 的 worker 上成立。）

**一处必须纠正的连带结论**：此前（连同 fork 侧静态审计）认为
"clone3 的 TOCTOU 不可达，因为外层 profile 对 clone3 直接返回 `ENOSYS(38)`"。
**实测这个前提不成立**：`s8`/`s11` 里 `clone3(NULL, 88)` 返回的是 **`EINVAL`**（内核的参数校验），
不是 `ENOSYS`。两者来源不同、不可混淆 ⇒ **外层的 clone3 规则在 arm64 上没有触发**。
生产环境里 `clone3` 是**真的能到达内核**的。

因此 `resource.rs:114-116` 那个"从 racy 用户内存读 flags"的检查，是 clone3 路径上
**唯一**的命名空间禁令执行点；`resource.rs:40-45` 的注释
（"never used as a security boundary"）与代码用途**直接矛盾**。
今天安全**完全依赖 CAP_SYS_ADMIN 被清除这一件事**，而不是依赖注释里说的机制。

⇒ 定级从 Med 降到 Low，但**依赖关系要说清**：一旦 `E2B_REAL_ROOT=0`
（`deploy/k8s/worker.yaml:405` 与 `envd_service/config.py` 仍支持该配置），
`drop_cap_sys_admin()` 不再执行（它在 `if sandbox.real_root` 里），
内层 mask 的漏项 + clone3 的 TOCTOU 会**同时**变成活的问题。

> 复现过程中的一个教训（不是漏洞）：`clone3` 且 `flags=0` 就是 `fork()`，
> 子进程会带着同一个栈返回 Python 解释器继续执行 ⇒ 递归 fork。
> 首版探针因此挂起，被平台的进程上限 + 沙箱超时兜住（事后核对：9 个 pod 全部
> `Running`、`RESTARTS 0`，节点 `Ready`，无残留沙箱）。修正版让子进程
> `os._exit(0)` 立即退出。**若要复现 clone3，务必这样做。**
> 另：ctypes 变参调用必须显式 `ctypes.c_long`，否则 64 位堆地址被截断成 32 位
> ⇒ 假 `EFAULT`。这个坑会让"内核未实现"和"参数非法"两种结论混淆。

### 5.4 STATIC-4 —— 门禁缺失确证，但生产形态下**观察不到泄露**

`handle_chroot_readlink`（:2493）、`handle_chroot_statfs`（:2899）、`handle_chroot_chdir`（:2750）
不做 `can_read` / `can_write` / deny 门禁，而兄弟 handler 都做。现场验证：

| 探测 | 结果 |
|---|---|
| `readlink /etc/localtime` | `OK /usr/share/zoneinfo/Etc/UTC`（**镜像自己的文件**，不是宿主） |
| `readlink /var/lib/e2b/state` | `13 Permission denied` ✔ 越界被夹住 |
| `statfs /`、`/etc`、`/var/lib/e2b`、`/var/lib/e2b/state` | 四者**完全相同**：`bsize=4096 blocks=262144` |
| `chdir /proc`、`chdir /sys`、`chdir /etc` | 全部 `OK`（门禁确实缺失） |
| `chdir` 到上述目录后 `statfs '.'` | 仍是同一个常数 |

`statfs` 四条路径返回**同一个值** ⇒ 它是沙箱自己的账目合成值（磁盘 stats handler
抢先应答），**不是宿主真实容量**，因此不存在容量/配额侧信道。
`chdir` 能进被拒子树属策略完备性缺口，但后续的受管 syscall 仍会各自把关。

⇒ **定级 Info。** 生产 real-root 形态下这三处没有可观察的安全影响，仅应作为代码一致性债务记录。

### 5.5 STATIC-5 —— **本地编译真实特权二进制实证**（✅ 已修复，见 remediation §8.2）

探针 `tmp/audit3/repro_c3_maint.sh` 用 `cc c3_agent/priv/maint.c c3_agent/priv/priv_common.c`
在**本机**编出真实的 `e2b-maint`，在一个假 roots 树上跑。**不触碰集群。**

| 用例 | 命令 | 实测输出 |
|---|---|---|
| 对照：`--uid` 走 pool 门禁 | `chown --uid 1 --path <root>/workspaces/victim-sandbox` | `refused: uid 1 is outside the privileged helper uid pool 10000..10999` ✔ |
| **实验：`--worker` 不走 pool 门禁** | `E2B_BROKER_WORKER_UID=1 e2b-maint chown --worker --recursive --path <root>/workspaces/victim-sandbox` | **`recursive chown ... to 1:65534 failed`** ⇒ uid=1（池外）**一路走到 `lchown` 系统调用** ✘ |
| 同上，打平台自己的账目树 | `--path <root>/state/_runtime` | 同样走到 `lchown`（`to 1:65534`）✘ |
| 同上，打别的租户的 secrets | `--path <root>/images/secrets` | 同样走到 `lchown` ✘ |
| **`walk` 非严格包含** | `walk --path <root>/workspaces`（root 本身） | **成功列出整棵树**：`attacker-sandbox`、`victim-sandbox`、`victim-sandbox/sub/secret.txt` 的 kind/uid/gid/mode/size 全给 ✘ |
| 穿越：`..` 出 root | `chown --path <root>/workspaces/victim-sandbox/../../../etc` | `refused: cannot resolve ...` ✔ |
| 穿越：`..` 回到 root | `chown --path <root>/workspaces/victim-sandbox/../..` | `refused: ... is outside the privileged helper roots` ✔ |
| 穿越：`walk` + `..` | `walk --path <root>/workspaces/../../..` | 同上，refused ✔ |
| **符号链接逃逸** | `chown --path <root>/workspaces/escape-link`（`escape-link -> /tmp`） | `refused: ... is outside the privileged helper roots` ✔（`realpath` 先解 symlink） |
| 符号链接逃逸 | `walk --path <root>/workspaces/escape-link` | refused ✔ |

**读法（重要，别读过头）**：本机非 root，所以 `lchown` 本身以 `EPERM` 失败、文件属主没变。
**这不影响结论** —— 报错信息本身证明了 uid=1（池外）**没有被 pool 门禁拦下**，
而是流到了 `lchown`。对照组的报错（`uid 1 is outside the ... uid pool`）来自门禁本身，
两者来源不同。真实节点上 face B 是 **uid 0 + `CAP_CHOWN`**，同一个调用会**成功**。

⇒ **确认**：`c3_agent/priv/maint.c:287-300` 的 `--worker` 分支不调 `priv_validate_uid()`，
而 `E2B_BROKER_WORKER_UID` 来自请求体 ⇒ 持 token 者可用
`worker_owned + recursive` 把 5 个 root 下**任意树**交给**任意非 0 uid**。
`priv_env_worker_identity` 只要求"非负十进制整数"，`app.py:185` 的 `Field(ge=1)` 是唯一的 0 号防线。

**同时确认干净**：**`..` 与符号链接逃逸 4/4 全部被拒**。`realpath` 前后缀包含检查 +
`FTS_PHYSICAL` + `lchown` 的组合是真的有效，不是纸面声明。

`app.py:611-622` 的 uid-全来自请求体（agent 侧无 sandbox→uid 绑定）保持静态判断，
**可达性实测为负**（§4：netpol 挡住 + token 不在 worker 镜像里）。
⇒ 这三条的定位是**"凭据一旦泄漏即等于该节点全量数据沦陷"的爆炸半径问题**，
不是当前可从沙箱直达的逃逸。

### 5.6 STATIC-6 —— 现场确证

`kubectl get daemonset e2b-c3-agent -o json` 实读：

```
pod 级:  hostPID=True  hostNetwork=None  hostIPC=None  automountServiceAccountToken=None
  container agent: runAsUser=65534 runAsGroup=None seccompProfile=null caps=add[SETUID,SETGID]
  container maint: runAsUser=0     runAsGroup=None seccompProfile=null caps=drop[ALL]+[CHOWN,DAC_OVERRIDE,FOWNER]
对照 worker:        seccomp={"localhostProfile":"sandlock-worker.json","type":"Localhost"}
```

值得单独指出的一条：**全舰队唯一的 uid 0 容器（`maint`）完全没有 seccomp 约束**，
而 worker 有 Localhost profile。它的攻击面 = HTTP 端点（token 门禁）+ 两个特权二进制。
建议给两个 face 都补 `seccompProfile`，并加 `readOnlyRootFilesystem` +
`automountServiceAccountToken: false` + face A 显式 `runAsGroup: 65534`。

---

## 6. 架构安全评估

### 6.1 分层结论

```
        ┌─────────────────────────────────────────────────────────┐
外部 ───▶│ 入口 (CP+gateway, :3000)   ← 本轮唯一的 Critical 在这  │
        └───────────────┬─────────────────────────────────────────┘
                        │  认证：仅 envd 的 access_token（可被客户端关掉）
        ┌───────────────▼─────────────────────────────────────────┐
        │ worker 容器 (uid 65534, drop ALL, Localhost seccomp)      │
        │   hostPath: workspaces / e2b-images / e2b/state          │
        ├──────────────────────────────────────────────────────────┤
        │ 沙箱 (pivot_root 真根, per-sandbox netns, Landlock,       │
        │       内核 seccomp blocklist, 每沙箱 uid 10000+)          │
        └───────────────┬─────────────────────────────────────────┘
                        │  ✗ 网络：私网/集群网段被拒
                        │  ✗ Calico netpol 挡住 c3-agent（唯一的 uid 0 组件）
                        ▼
              c3-agent face B (uid 0 + CHOWN/DAC_OVERRIDE/FOWNER, hostPID)
```

**隔离设计的核心判断是对的**：把唯一的 root 组件（c3-agent）用 NetworkPolicy +
无凭据可达性双重隔开，并且让它只接受 `app=control-plane` 来源。这条防线本轮**实测有效**。

### 6.2 真正的单点

**"谁能通过入口拿到哪个沙箱的路由"这件事，只由 envd 的一个可被客户端关闭的布尔量决定。**

架构里已经有正确的东西 —— c3-agent 那套"身份来自凭据、绝不来自请求体"的规则写得非常好
（§4 逐条验证）。但同一份设计文档里，**控制面自己的 envd 令牌却来自请求体**，
两者标准不一致。修 SEC-R3-01 的方向应该是**把 envd 也纳入 c3-agent 那条已经成文的规则**：
*身份来自凭据，不来自请求体*。

### 6.3 其它结构性观察（不修，仅记录）

1. **控制面与工作节点同机**：`izuf697v12g31dyz4uvsjlz` 同时是 control-plane 和 `e2b-worker-0` 的宿主。
   worker 容器一旦失守，爆炸半径直接覆盖 kube-apiserver 证书与 etcd 数据。
   2 节点集群里这不是"能不改"，但应当被记为已知风险并在容量/扩缩容时避免共置。
2. **namespace 无 default-deny**：只有 c3-agent 一条 NetworkPolicy。控制面、redis、
   gateway、worker 全裸奔在 pod 网络里。本轮证明沙箱够不到它们，但**控制面 RCE 会立刻
   够得到一切**（§4 STATIC-5：控制面持有 c3-agent token，而 netpol 恰好放行控制面）。
3. **`/internal/**` 与公开 API 同端口**：`172.18.78.49:3000` 同时提供公开 API 和
   `/internal/routes/{id}`（返回 worker 内网地址）。本轮用 tmp 里的 internal key 从集群外
   成功查到了 `{"nodeID":"e2b-worker-0","address":"http://10.244.140.6:49983"}`。
   **有 key 才能用**，但"内网拓扑信息"和"公网业务面"共用一个监听端口，建议拆分。

---

## 7. 复现方式

探针全部在 `tmp/audit3/`（gitignored，不入库）。

```bash
cd <仓库根>
export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"      # deploy/scripts/open-cluster-tunnel.sh 建通道
export E2B_API_URL=http://172.18.78.49:3000 E2B_SANDBOX_URL=http://172.18.78.49:3000
export E2B_API_KEY=$(cat tmp/audit/api_key)
```

| 探针 | 覆盖 | 读数 |
|---|---|---|
| `tmp/audit3/s1_notify_fd.py` | 遍历 fd + `NOTIF_ID_VALID`，判定是否持有 seccomp listener（§3.1） | 现场 stdout |
| `tmp/audit3/s2_reach.py` | 沙箱内目标可达性底图（默认策略下全拒） | `out-s2.json` |
| `tmp/audit3/s3_reach_allowout.py` | `allow_out` 授权后的可达性矩阵 | `out-s3-reach.json` |
| `tmp/audit3/s4_secure_flag.py` | `secure` 字段是否被接受、token 长度对比 | `out-secure-flag.json` |
| `tmp/audit3/s5_rce_proof.py` + `tmp/audit3/in_pod_rce.py` | **SEC-R3-01 主证据**：5 用例矩阵（集群内发起） | `out-rce-proof.json` |
| `tmp/audit3/s6_escalation.py` | 跨租户腿 + 公开暴露腿 | `out-escalation.json` |
| `tmp/audit3/s7_cross_tenant_reach.py` | 每个内网目标单独一个沙箱（避免单点挂起掩盖其余） | `out-cross-tenant-reach.json` |
| `tmp/audit3/s8_static_repro.py` | STATIC-2 的 7 个 `*at` syscall；STATIC-3 的 clone 标志；STATIC-4 的三个 handler | `out-s8.json` |
| `tmp/audit3/s10_caps_only.py` | **能力普查（只读）**：`capget` / `NO_NEW_PRIVS` / bounding set | `out-s10.json` |
| `tmp/audit3/s11_clone3_safe.py` | clone3 的层级归因（子进程立即 `_exit`，不会递归 fork） | `out-s11.json` |
| `tmp/audit3/s9_caps_clone.py` | ⚠ **已废弃**：裸调 `clone`/`clone3` 且 `stack=0` 会导致递归 fork 而挂起。保留仅为记录该坑，勿直接运行。 | — |
| `tmp/audit3/repro_c3_maint.sh` + `c3build/` | **本地**编译真实 `e2b-maint` 并复现 STATIC-5（pool 门禁绕过 / `walk` 非严格 / 穿越与符号链接） | stdout |
| `tmp/audit3/in_pod_rce.py` | SEC-R3-01 的集群内半边（Connect 信封协议） | — |

`tmp/audit3/repro_c3_maint.sh` 的用法（纯本地，不连集群）：

```bash
cc -O0 -o tmp/audit3/c3build/e2b-maint c3_agent/priv/maint.c c3_agent/priv/priv_common.c
sh tmp/audit3/repro_c3_maint.sh tmp/audit3/c3build/e2b-maint "$PWD/tmp/audit3/fakeroot"
```

> ⚠ fork 的 Rust 测试（`cargo test -p sandlock-core`）**在本机编不过** ——
> 该 crate 是 Linux 定向的，且 build.rs 需要一个可用的 C 编译器来编 restore stub。
> 规范跑法在 Linux 容器内（见 `third_party/sandlock/scripts/test-all.sh`）。

SEC-R3-01 的**入口侧**最小复现（集群外，零凭据）：

```bash
# 1) 建一个 secure:false 沙箱（记录 sandboxID）
curl -sS -X POST http://172.18.78.49:3000/sandboxes \
  -H "X-API-Key: $E2B_API_KEY" -H 'Content-Type: application/json' \
  -d '{"templateID":"base","timeout":900,"secure":false}'      # => envdAccessToken: ""

# 2) 造 Connect 信封（>BI 帧：flags=0, len, JSON）
python3 -c "
import json,struct
p={'process':{'cmd':'/bin/sh','args':['-c','id; hostname; echo PWNED']}}
b=json.dumps(p,separators=(',',':')).encode()
open('/tmp/e.bin','wb').write(struct.pack('>BI',0,len(b))+b)"

# 3) 经对外入口，**不带任何凭据**
curl -sS -X POST http://172.18.78.49:3000/process.Process/Start \
  -H "Content-Type: application/connect+json" \
  -H "E2b-Sandbox-Id: <上一步的 sandboxID>" \
  --data-binary @/tmp/e.bin
```

> 探针全部只读（`id` / `hostname` / `echo` / `cat /etc/hostname`），
> 唯一的副作用是创建并立即删除若干一次性沙箱；每支探针结束都会清理。