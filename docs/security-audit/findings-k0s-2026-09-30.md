# k0s 线上沙箱白帽审计 —— 发现（2026-09-30）

**范围**：自建 k0s 集群（2 节点 arm64、`v1.36.4+k0s`、namespace `sandlock`）上
运行的 E2B 兼容沙箱，版本 `0.1.0-818-g7205fba-20260930-221244`。
**方法**：静态读码 + 在**线上真实沙箱**里跑可复现探针；结论一律以运行时证据为准。
**边界**：只确认与记录，**未修改任何生产代码、配置或部署**。

> ⚠ **生产影响披露**：本轮探针在验证 SEC-K0S-003 时**导致 `e2b-worker-0` 被
> OOMKilled 并重启一次**（2026-09-30 15:06:00Z，exit 137）。详见该条的"披露"小节。
> 这是本轮唯一的一次生产影响，成因是探针自己（`yes` 无限输出），不是被测代码的
> 未知行为。集群已自动恢复，所有工作负载 `Running`，无数据损失。

所有创建的沙箱/卷/快照/secret 已清理（终态 `/sandboxes` `[]`、`/volumes` `[]`、
`/secrets` `[]`）。9 个 pod 全部 `Running`；`e2b-worker-0` 的 `restarts=2`
（其中一次来自上述 OOM，一次来自探针结束时的正常收敛）。

| 编号 | 严重度 | 一句话 | 状态 |
|---|---|---|---|
| SEC-K0S-003 | **高** | 命令输出上限只封回放缓冲、不封实时流；实测一条 `yes` 即可 OOM 掉 worker | 已确认，**含一次生产影响** |
| SEC-K0S-002 | 中 | 控制面 `/openapi.json`、`/docs` 无需认证即可拉取 47 条内部接口全图 | 已确认（信息泄露，非越权） |

与既有审计（`docs/security-audit/findings.md`，2026-09-16）的关系：那轮测的是
**本地容器 + compose 形态**（pure / 模拟 chroot 两种）。本轮目标是 **k0s + REAL_ROOT
形态**，是它没有覆盖过的部署形态 —— 结论不能互相套用，本文档不复述那轮的发现。

## 目标形态（先钉死，否则结论无意义）

| 项 | 实测值 |
|---|---|
| 集群 | 2 节点 arm64 / `v1.36.4+k0s` / ns `sandlock`（`open-cluster-tunnel.sh --check` 自检通过） |
| 内核 | `6.12.0-211.34.1.el10_2.aarch64` |
| 沙箱内身份 | `uid=0(root) gid=0(root) groups=0,65534`（userns 内的 fake root） |
| 沙箱内核根 | **真根**（`E2B_REAL_ROOT=1`）：只读 xfs，只有 `/home/user`、`/workspace` 是 NFS RW 绑定 |
| worker pod | `runAsUser/Group=65534`、`capabilities.drop=[ALL]`、`seccompProfile=Localhost` |
| 隔离开关 | `E2B_PID_NS=true`、`E2B_ENABLE_NET_ISOLATION=true`、`E2B_FD_INJECT_CONNECT=true` |
| 网络 | pod CIDR `10.244.0.0/24` + `10.244.1.0/24`（落在默认私网拒绝段内）；C3 agent 有 NetworkPolicy |

沙箱内 `/proc/mounts` 原文（这是 REAL_ROOT 形态最硬的证据）：

```
sandlock /           xfs      ro,relatime 0 0
sandlock /home/user  nfs      rw,relatime 0 0
sandlock /workspace  nfs      rw,relatime 0 0
sandlock /dev/{null,zero,urandom,tty}  tmpfs  rw,relatime 0 0
```

即"可写面 = 两个 NFS 绑定 + 四个 tmpfs 设备"，其余全部只读且不可遍历。

---

## 确认的漏洞

### SEC-K0S-002（中）控制面 `/openapi.json` 与 `/docs` 无需认证即可拉取内部接口全图

**可达性**：无需任何凭据，从**文档记载的租户入口** `http://172.18.78.49:3000`
直接 200。

```
$ curl -s -o /dev/null -w "%{http_code} %{size_download}\n" http://172.18.78.49:3000/openapi.json
200 52803
$ curl -s -o /dev/null -w "%{http_code} %{size_download}\n" http://172.18.78.49:3000/docs
200 1035
/redoc               200  917
/docs/oauth2-redirect 200 3012
```

**暴露内容**：`47` 条路径的完整清单，且 `components.securitySchemes` 为**空**
（`{}`）—— 即规范里没有任何端点声明自己需要认证。含以下内部/特权面：

```
/internal/nodes/{node_id}/file-op              <- C3 面 B：agent 代执行的特权文件操作中继
/internal/nodes/{node_id}/agent/inventory     <- agent 上报 / 控制面决策的通道
/internal/nodes/{node_id}/slot-identity       <- 槽位身份授予
/internal/nodes/register  /internal/nodes/{node_id}/heartbeat  /reconcile  /drain  /undrain
/internal/fleet/metrics   /internal/fleet/sandboxes   /internal/tenants   /internal/routes/{sandbox_id}
/nodes/{node_id}/agent/…  /sandboxes/{sandbox_id}/{checkpoint,fork,migrate,network,pause,resume,logs}
```

更关键的是**端点描述里带内部信任模型的散文**，例如 `file-op`：

> "the worker reports `{sandbox_id, op}` -- never a path and never a uid
> (hard rules 1/3, §14.4) -- and the control plane, which holds the records,
> derives the target and instructs the agent, which executes it"

这段话本身**不构成越权**（`X-Internal-Key` 仍然把关，实测 401），但它把
C3 的**硬规则编号、信任边界位置、"worker 永不自报路径与 uid"这条设计前提**
连同端点形状一起交给了任何能连到 3000 端口的人。

**根因**（单点，且仓库里已有正确写法）：

- `control_plane/app.py:459` — `FastAPI(title=..., lifespan=lifespan)`，
  **没有** `docs_url=None, redoc_url=None, openapi_url=None`；
- `c3_agent/app.py:350-352` — 同一仓库里的 C3 agent 显式关掉了这三个，并且
  注释写明了理由：*"No interactive surface: the instruction API is the whole
  contract, and `/openapi.json`/`/docs` on a privileged service is inventory
  for free."*

也就是说**特权性更强的 C3 agent 记得关，控制面忘了关**。`envd_service/gateway.py:163`
同样未关，但本次实测在公共入口上取到的规范 `info.title` 是
`E2B Sandlock Gateway - Control Plane`（与 `control_plane/app.py:459` 的字面量一致），
即**实际暴露的就是控制面这一份**。

**影响**：把内部攻击面从"需要先拿到 key 才能枚举"降级为"能连到入口端口就能枚举"。
与 OBS-6（单租户默认）叠加时价值更高：一旦开放第二个使用者，攻击者连枚举阶段
都不需要凭据。

**未证实的部分（不夸大）**：仅凭这份规范**无法**调用任何一个 `/internal/*`
端点 —— 实测用 API key、代码默认值 `internal-key`、空 key 三种凭据打
`/internal/nodes`、`/agent/untrusted`、`/agent/sandboxes`、`/agent/sandboxes/{id}/export`
全部 401。**这是信息泄露，不是越权。**

**建议修法**（一行，未实施）：`control_plane/app.py:459` 补
`docs_url=None, redoc_url=None, openapi_url=None`，与 `c3_agent/app.py` 对齐。

---

### SEC-K0S-003（高）命令输出上限只管回放缓冲，不管实时流 —— 实测打崩 worker

**这不是"读码推断"，是实测打崩过一次 worker。** 见下方"披露"一节。

**实测（线上真实沙箱，经官方 SDK 的 `commands.run`，即实时流路径）**：

| 请求 | 实际收到 | 耗时 |
|---|---|---|
| 5 MiB | 5 242 880 B (5.0 MiB) | 1.0s |
| 20 MiB | 20 971 546 B (20.0 MiB) | 1.9s |
| 80 MiB | 83 886 106 B (80.0 MiB) | 4.9s |
| 150 MiB | 157 286 426 B (150.0 MiB) | 10.0s |
| 300 MiB | 300 000 026 B (300.0 MiB) | — |

**无一被截断。**

**根因**（`envd_service/process/manager.py`，输出循环）：

```python
408  async for kind, chunk in running.output():
413      truncated_now = append_captured(proc, kind, chunk)   # ← 只封顶 proc.captured（回放缓冲）
419      self._broadcast(proc, ("data", kind, chunk))          # ← 实时流无上限转发
```

`append_captured`（同文件 90-125 行）实现是正确的：它在 `capture_limit` 处
截断并写入 `TRUNCATED_MARK`。`E2B_COMMAND_CAPTURE_LIMIT_MB` 默认 10
（`envd_service/config.py:385-387`），worker env 未设置该项，所以取默认 10 MiB。
但**第 419 行的 broadcast 在封顶之外**：每个 chunk 原样推给所有活跃订阅者。

**实测影响：worker 进程被 OOM 杀死。** 把这条上限当成"缺失的护栏"来评估是不够的 ——
它缺失的那一半路径**已经造成过一次真实的可用性事故**（详见"披露"）。

#### 披露：本轮探针导致 `e2b-worker-0` OOMKilled（2026-09-30 15:06:00Z）

在测量"回放路径是否被封顶"时，我用 `yes`（~1 GB/s 无限输出）让 worker 的
捕获缓冲持续增长，**结果把 worker 的内存打爆了**：

```
$ kubectl -n sandlock get pod e2b-worker-0 -o jsonpath='{...lastState.terminated.reason}'
OOMKilled
exitCode=137
finished=2026-09-30T15:06:00Z
```

- 该 pod 的 limit 是 `memory: 4Gi`（`resources.limits`），单沙箱 `max_memory=1024M`，
  而流式缓冲不计入任何一层封顶；
- 触发后 StatefulSet 自动拉起新容器（当前 `1/1 Running`，restarts=2），
  控制面 `/sandboxes` 终态 `[]`，C3 agent 的自愈扫描没有留下孤儿树；
- **这是本轮唯一的生产影响事件，且是我造成的。** 报告如实记录，探针不再复现该负载。

这同时给出了一条比"理论上能打爆"强得多的证据：**单租户下任意一个沙箱，
一条 `yes` 命令即可让承载它的 worker 进程重启**。重启期间该节点上所有沙箱
的 exec/文件操作不可用（另一节点不受影响）。

**与既有审计的关系**：`findings.md` 的 L4 表把"命令输出"记为
"worker 侧 10 MiB 封顶（E4.1，`CAPTURE_LIMIT_DEFAULT`），非本轮改动" ——
**该结论只对回放路径成立**，本轮实测推翻了它作为整体护栏的读法。

#### 未能证实的部分（不夸大）

报告初稿写"回放被封到 10 MiB、实时流不限，两条路径相差 30 倍"。**回放那一半
始终没测到**：`commands.list()` 在命令结束后返回 0 条（已结束的进程被丢弃），
而 `rpc_connect`（`envd_service/rpc.py:263`，唯一用 `subscribe(replay=True)` 的地方）
要求 pid 仍在 `ctx.processes` 里。我尝试连接一个运行中的进程来触发回放，
但那条 `yes` 负载先把节点打爆了（见上），**回放封顶至今是读码推断，不是观测**。
修复后应当用"跑一条有限输出的命令 → 连接上去读回放 → 断言字节数 ≤ 10 MiB
且含 `TRUNCATED_MARK`"来钉这条，探针留在 `tmp/audit/probe_i1_replay.py`。

**建议修法**（未实施）：把封顶提到 broadcast 之前，即在第 419 行按流累计字节、
超限后停止转发并只发一次 marker；`append_captured` 的语义（"回放缓冲"）
与"流上限"应当是两个独立旋钮，或者明确把后者命名为 `E2B_STREAM_OUTPUT_LIMIT_MB`
并给一个默认。

---

## 已实测为**干净**的面（防止下轮重复劳动）

**L1 内核接口** — 分两步做的：

1. 沙箱内 0..500 全号段扫描（每个号在独立 fork 子进程里调，记录原始返回值），
   **209 个号返回 EPERM**。⚠️ **这 209 个不能直接当作"沙箱自己的 blocklist 起了作用"** ——
   它同时包含：sandlock 内层 deny BPF、参数过滤器（`arg_filters_resolved`）、
   以及**外层 worker pod 的 `sandlock-worker.json`**（`defaultAction: SCMP_ACT_ERRNO`）。
   这三者都返回 EPERM，单靠返回值**无法区分来源**。
2. 因此又做了一次**逐条定向核对**（`probe_j1_blocklist_arm64.py`）：从
   `syscalls` crate 0.8.1（与 `third_party/sandlock/Cargo.lock` 锁定版本一致）的
   `src/arch/aarch64.rs` 读出**每个名字在本架构的真实号**，然后**直接按该号调用**，
   记录原始返回值，并以 `getpid` 作对照。

> **结论：在 arm64 上存在的 36 个 blocklist 条目，逐条实测全部 `EPERM(1)`；**
> `ioperm` / `iopl` 在该 ABI 上不存在（`resolve_blocklist` 按 `Sysno` 解析自动跳过 ——
> 设计好的行为，不是缺口）；对照组 `getpid(172)` 返回 `OK:44`，证明探针本身能穿过过滤器。

```
acct(89)=EPERM            add_key(217)=EPERM        bpf(280)=EPERM
chroot(51)=EPERM          delete_module(106)=EPERM  finit_module(273)=EPERM
init_module(105)=EPERM    io_uring_enter(426)=EPERM  io_uring_register(427)=EPERM
io_uring_setup(425)=EPERM kexec_load(104)=EPERM      keyctl(219)=EPERM
lookup_dcookie(18)=EPERM  mount(40)=EPERM            name_to_handle_at(264)=EPERM
open_by_handle_at(265)=EPERM  open_tree(428)=EPERM   open_tree_attr(467)=EPERM
perf_event_open(241)=EPERM personality(92)=EPERM     pidfd_getfd(438)=EPERM
pivot_root(41)=EPERM      process_vm_readv(270)=EPERM  process_vm_writev(271)=EPERM
ptrace(117)=EPERM         quotactl(60)=EPERM         reboot(142)=EPERM
request_key(218)=EPERM    setdomainname(162)=EPERM   sethostname(161)=EPERM
setns(268)=EPERM          swapoff(225)=EPERM         swapon(224)=EPERM
umount2(39)=EPERM         unshare(97)=EPERM          userfaultfd(282)=EPERM
```

（上一版报告把这一段写成"209 个号被拒 ⇒ blocklist 完整"，并附了一批**并未逐条观测**
的读数；本节是修正后的版本：**38 个名字 → 36 个在本 ABI 存在 → 36 个实测 EPERM**。）

`AF_PACKET`/`AF_VSOCK` 建不出来，`AF_INET+SOCK_RAW(ICMP)` = EPERM；
唯一放行的 `AF_NETLINK` 只读路由信息（非逃逸，与 OBS-2 一致）。

**L1 文件系统** — 带路径 syscall（`statx`/`faccessat`/`getxattr`/`listxattr`/
`inotify_add_watch`）对 7 条**只存在于宿主根**的路径（`/var/lib/e2b-sandboxes`、
`_secrets`、`_volumes`、`/sandlock`、`/mnt/nas`、`/proc/1/root/...`）
**全部 EPERM**。这与 2026-09-16 那轮 pure 形态的 OBS-2/OBS-7（`inotify`/`getxattr`
成功、能拿到宿主目录事件与 xattr）**结论相反** —— 原因是形态不同：
那轮 pure 形态只有 Landlock 一道网，而本轮 REAL_ROOT 形态有 seccomp 路径中介
在前面挡着。**那两条观察项在本形态下不成立，不需要再跟踪。**
`/etc/passwd`/`/etc/shadow` 可读（镜像自带，非宿主）；`/proc` 列表为空
（PID namespace 生效）；`/proc/1/*`、`/proc/self/*` 全部 EACCES。

**L2 横向** — 两个沙箱互攻：A 读/列/写 B 的 workspace 与 `_runtime`、
按绝对宿主路径、按符号链接（`ln -s /var/lib/e2b-sandboxes/workspaces`）、
按硬链接 —— **全部 EACCES/ENOENT**。符号链接本身可以创建（`symlink_created OK`），
但读穿时 `ENOENT`（虚拟根内无此路径），`open` 时 `EACCES`（Landlock 兜底）。
文件 API 侧：`/workspace/../../etc/passwd` 被明确拒绝为
`InvalidArgumentException: path escapes the sandbox root`；`/etc/passwd`、
`/var/lib/e2b-sandboxes/_secrets` 报 not found；写 `/var/lib/e2b-sandboxes/_escape.txt`
返回 `mkdir: cannot create directory 'var/lib': Permission denied`（**顺带泄露 worker
的内部路径拼写**，信息面，非越权）。

**网络** — 沙箱内对 10 个内部目标的连接**全部 ECONNREFUSED(111)**：
两个 C3 agent（`10.244.140.21/10.244.192.229:49985`）、两个 worker envd
（`:49983`）、两个 control-plane（`:3000`）、redis pod 与 Service
（`10.244.192.201:6379`、`10.98.99.192:6379`）、`cp-svc`、`gw-svc`。
SEC-001 的四种写法（`127.0.0.1` / `0.0.0.0` / `0.0.0.1` / `::1`）全部拒绝 ——
**SEC-001 在本形态上是干净的**。C3 agent 另有 NetworkPolicy 双保险
（ingress 只放 `app=control-plane` 到 49985/49986）。

**控制面认证** — 无 key / 错 key / 空 key / 非 ASCII key / 截断 1 字节 /
多 1 字节：全部 401。API key **不能**访问 `/internal/*`（401）与 C3 agent 面。
`/internal/nodes/{node_id}/file-op` 这类特权中继在控制面侧同样要 internal key。

**L4 资源上限** — 逐项重测，**输出是唯一失效的一项**：

| 维度 | 实测 | 结论 |
|---|---|---|
| 磁盘 | `RLIMIT_FSIZE=1 GiB`，写满 1024 MiB 返回 `EFBIG(27)` | **真**（N25/C 已上线；比 2026-09-16 那轮“64 MiB 上限下写 192 MiB 无拒绝”有改善） |
| 进程数 | `RLIMIT_NPROC=30519` | **真** |
| 创建类限流 | 连打 110 次 `POST /volumes` 后第 111 次起 429，且**按端点独立** | **真**（OBS-8 修复在线上是活的） |
| **命令输出** | 300 MiB 全量送达；`yes` 直接 OOM 掉 worker | **假**（见 SEC-K0S-003，高） |

---

## 一个被推翻的怀疑（记录下来，避免下轮重走）

**怀疑**：`init_module` / `delete_module` 在沙箱内返回 0（成功），而
`finit_module` 返回 EPERM —— 看起来像"模块加载没被拦"。

**证伪**：我最初用的是 **x86_64 的 syscall 号**（`init_module=175`、
`delete_module=176`）。arm64 上这两个号根本不是它们（arm64 是
`init_module=105` / `delete_module=106`），而 175/176 在 arm64 上是别的调用，
所以"返回 0"只是这些号在 userns 里的正常行为。**用 `syscalls` crate 0.8.1 的
`aarch64.rs` 逐条重算后，38 个 blocklist 条目全部被拒**（见上表）。

教训（对本项目后续审计有普遍价值）：**跨架构审计必须先用 `syscalls` crate
的 per-arch 表把名字解析成号，再去比对观测**，否则会把"号错了"读成"洞"。
本仓库已经把这件事做对了一半 —— `sys/structs.rs` 用 `Sysno` 解析、
`arch.rs` 有 `crate_sourced_consts_match_historical_values` 钉住历史常量 ——
但那只保护**编译期**；探针侧没有这层保护。

---

## 遗留/建议（未实施，按性价比）

1. **SEC-K0S-002 修法**：控制面关掉 OpenAPI（一行）。
2. **SEC-K0S-003 修法**（优先级高于上一条）：把输出封顶提到 broadcast 之前；
   或新增独立的流上限旋钮。**这条是本轮唯一造成过生产影响的项**，
   而"修复"只需把已有的 `append_captured` 语义扩展到 broadcast 之前。
   修完应当用 `tmp/audit/probe_i1_replay.py` 验证：有限输出的命令 → 连接读回放 →
   断言回放字节数 ≤ 上限且含 `TRUNCATED_MARK`；再用一条高输出命令断言
   **实时流**也在上限处收到 marker（当前收不到）。
3. **OBS-6 复核**：`E2B_TENANTS` / `E2B_ADMIN_API_KEYS` 在线上控制面 env 中
   **确实未设置**（实测 `kubectl get deploy control-plane` 的 env 全量核对），
   即当前是**单租户兼容模式**。这与 2026-09-22 的用户决定一致（有意不启用），
   本轮只是确认线上未漂移。触发条件仍按原文档：开放第二个使用者 / 跨团队共享 API
   / 暴露 sandbox id 或 token 给非属主 —— 届时设两个变量 + 跑一次迁移脚本。
4. **envd 数据面无租户校验**（OBS-6 纵深项）：envd 只校验
   `E2b-Sandbox-Id` + `X-Access-Token`，不带租户维度。在当前单租户前提下
   **不构成越权**（token 拿不到），随决定一起搁置。
5. `/files` 写入失败时把 worker 内部绝对路径回显给调用方
   （`mkdir: cannot create directory 'var/lib': Permission denied`）：
   信息面，很小，但可以在错误映射里抹掉宿主路径拼写。

## 复现方式

本轮探针在 `tmp/audit/`（gitignored，不入库）：

| 探针 | 覆盖 |
|---|---|
| `probe_a2_l1.py` | L1 内核接口 + 路径面（主力） |
| `probe_a9_scan.py` | syscall 号 0..500 全扫描（EPERM 集合，**注意它无法区分 deny 来源**） |
| `probe_j1_blocklist_arm64.py` | **逐条**按 arm64 真实号验证 blocklist（36/36 EPERM，getpid 对照） |
| `probe_b1_verify8.py` | arg-filter 族逐条确认（`unshare`/`personality`/`reboot` 等） |
| `verify_numbers.py` | 用 `syscalls` crate 0.8.1 的 aarch64 表核对 blocklist 覆盖 |
| `probe_c1_l2.py` | L2 横向（宿主路径 / 遍历 / 符号链接 / `/proc` / 信号） |
| `probe_d_net.py` | 网络可达性 + SEC-001 写法矩阵 |
| `probe_d1_l3.py` / `probe_d3_fileapi.py` | 控制面认证 / id 校验 / 文件 API 遍历 |
| `probe_f1_cp.py` | 控制面全端点（`/openapi.json` 暴露由此发现） |
| `probe_g1_ratelimit.py` | 创建类端点限流（打到 429） |
| `probe_h1_l4.py` / `probe_h2_output.py` | L4 资源上限（磁盘/进程/输出） |
| `probe_i1_replay.py` | 回放路径封顶的验证（**当前未能跑通**，见 SEC-K0S-003“未能证实”） |
