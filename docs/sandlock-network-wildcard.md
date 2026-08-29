# sandlock 扩展方案：网络规则域名通配符（与 E2B 标准一致）

> 状态：提案。目标版本基线：sandlock 0.8.6（main 分支 2026-08-23，含
> credential injection 但未发版）。

## 1. 背景与目标

E2B 官方 `network.allowOut` 把 `*.example.com` 列为合法条目，且**与是否配置
`egressProxy` 无关**——官方平台的所有出站流量都经过统一的域名规则引擎。

本项目当前：

- `egressProxy` 模式：通配域名已支持（LD_PRELOAD 库内 `*.suffix` 过滤）；
- 普通模式：显式 400（sandlock `net_allow` 无法表达后缀通配）。

目标：**普通模式也支持 `*.example.com`，且不降低安全模型**（保持 sandlock
内核级强制，沙箱 unaware、静态/Go 应用同样受限），与 E2B 标准一致。

## 2. 为什么必须改 sandlock（不能走 LD_PRELOAD）

普通模式的过滤由 sandlock 的 seccomp user-notification 在 supervisor
`connect_on_behalf` 代连路径执行，是**内核级、不可被沙箱绕过**的强制。

LD_PRELOAD 是沙箱进程内的用户态 hook：

- 静态链接 / Go / 自实现 syscall 的应用不受影响（可绕过规则）；
- 沙箱内可检测并规避（`LD_PRELOAD` 可被清空、`dlsym(RTLD_NEXT)` 可被绕开）；
- 违反 E2B "sandbox unaware / 平台强制" 语义。

因此通配域名规则必须下沉到 sandlock 的 on-behalf 路径。

## 3. 现状：sandlock 0.8.6 网络模型（源码依据）

| 模块 | 职责 | 与通配域名的关系 |
|---|---|---|
| `crates/sandlock-core/src/network/rules.rs` | `--net-allow/--net-deny` 解析；hostname **创建时 DNS 解析进 IP 集合**；虚拟 `/etc/hosts` 组装 | hostname 规则在创建时被"固化"成 IP，无法表达动态子域 |
| `crates/sandlock-core/src/network/verdict.rs` | `destination_verdict(ip, port)` 纯函数 | 只拿到 IP，无 hostname 上下文 |
| `crates/sandlock-core/src/network/materialize.rs` | sockaddr 解析（TOCTOU 安全） | 同上 |
| `crates/sandlock-core/src/network/connect.rs` | `connect_on_behalf`：策略判定 + `ConnectPlan`（redirect/remap/passthrough）→ 代连 | 决策点是 IP，扩展点在这里 |
| `crates/sandlock-core/src/seccomp/notif.rs` | `NetworkPolicy::{AllowList{per_ip,cidrs,any_ip_ports}, DenyList, Unrestricted}` | `per_ip` 是 hostname 解析后的落点 |
| `crates/sandlock-core/src/netlink/synth.rs` | 虚拟网卡/地址的 netlink 合成 | 已具备"虚拟网络视图"能力，DNS 合成可复用思路 |
| `python/src/sandlock/sandbox.py` | Python 参数 `net_allow` 等 → FFI | 规则语法扩展的透传点（无 Python API 破坏） |

关键结论：sandlock 的 hostname 规则 = "创建时解析 → 固定 IP 集合"。要支持
`*.example.com`，必须让 supervisor 在**连接时**知道目标 hostname 并做后缀
匹配。

## 4. 需求点（编号 + 验收）

### R1 — 规则语法：`net_allow` 接受通配域名

- `net_allow` 条目支持 `*.example.com`（仅 TCP/UDP 目标规则，语法沿用
  `host:port` 后缀）。
- 语义：匹配任意**子域**（`api.example.com`、`a.b.example.com`），**不匹配
  裸域名** `example.com`（与 egress-proxy 库 `*.suffix` 语义一致）。
- 与字面 hostname 规则、IP/CIDR 规则共存；`net_deny` 保持仅 IP/CIDR
  （E2B 标准 deny 不支持域名）。
- 验收：`Sandbox(net_allow=["*.example.com:443"])` 可创建；非法形态
  （`**`、空后缀、带 scheme 的通配）报清晰错误。

### R2 — 沙箱内 DNS：通配域名的子域解析返回合成 IP

- 沙箱内 `getaddrinfo("api.example.com")` 返回一个**专用合成 IP**（如
  `127.0.0.2/8` 段，与 egress-proxy 库同段或独立段），不触发真实 DNS；
- supervisor 维护 `hostname ↔ 合成 IP` 映射（线程安全、容量上限、LRU 或
  按规则域分组）；
- 非通配 hostname 规则保持现状（创建时解析 pin 真实 IP）。
- 验收：沙箱内解析命中通配域的子域返回合成 IP；映射可查询；并发解析无
  竞态。

### R3 — on-behalf connect：合成 IP 反查 hostname 并匹配

- `connect_on_behalf` 在拿到目标 IP 后：若是合成 IP → 反查 hostname →
  按 `*.suffix` 规则匹配（allow 命中才继续）；
- 匹配通过后：supervisor **实时解析该 hostname**（保持 E2B "远程 DNS /
  dial-time 解析" 语义）并代连；解析失败 → `ECONNREFUSED`（fail closed）；
- 未命中任何通配规则、或沙箱直接连接字面 IP：走现有 `NetworkPolicy` IP
  判定，**不允许借合成 IP 段绕过字面规则**。
- 验收：通配子域连接成功且目标正确；非匹配子域被拒；字面 IP 连接仍受
  allow/deny 约束；静态/Go 客户端（不走 libc getaddrinfo 的直连 IP）无法
  绕过。

### R4 — 合成 IP 段的防绕过

- 合成 IP 段对沙箱只用于"承载 hostname"：任何直连合成 IP（不经过
  hostname 反查）必须拒绝，且合成 IP 永不作为真实出口地址；
- 沙箱内无法把任意 hostname 注册进映射（映射只由 supervisor 在解析
  getaddrinfo 时写入）；
- 验收：沙箱内 `connect(合成IP:任意端口)` 不匹配任何规则时返回
  `ECONNREFUSED`；模拟恶意直接写合成 IP 无法建立出站。

### R5 — HTTP ACL / rules 域名的通配（可选，随 R1-R4 一并设计）

- `http_allow` / `http_deny` 的 host 位置支持 `*.example.com`（当前仅
  `*` 任意）；为 B2（credential injection 按域名注入）铺路。
- 验收：`http_allow=["GET *.example.com/*"]` 拦截并放行子域、拒绝裸域。

### R6 — 项目接入：校验放开 + 策略映射

- `gateway_common/network.py`：普通模式下 `allowOut` 允许 `*.example.com`
  （不再依赖 `egressProxy`）；
- `SandlockExecutor`：把通配条目原样传给 sandlock `net_allow`（不展开、
  不转换）；普通模式不再需要 LD_PRELOAD 参与过滤。
- 验收：`POST /sandboxes` 普通模式带 `allowOut: ["*.example.com"]` 返回
  201 并回显；sandlock 沙箱内子域可达、裸域拒绝。

### R7 — 兼容与回归

- 现有字面 hostname / IP / CIDR / bind / egressProxy / rules 行为不变；
- 单机与多节点（共享 workspace、迁移、Redis）回归全绿；
- 动态更新（`PUT /sandboxes/{id}/network`）下一条命令生效。

## 5. 技术方案（逐文件改动）

### 5.1 `network/rules.rs` — 规则解析

- `NetRule` 增加通配 hostname 变体：`*.suffix`（剥离 `*.` 存 suffix，
  校验：suffix 至少一个 `.`、无其他 `*`）。
- 解析阶段：通配规则**不 DNS 解析**，进入新的策略字段
  `wildcard_suffixes: Vec<HostSuffixRule>`（连同端口）；字面 hostname 规则
  维持现状（解析进 `per_ip`）。
- `/etc/hosts` 虚拟化：通配规则不再参与字面 pin；DNS 拦截逻辑改由
  supervisor 处理子域解析（见 5.2）。

### 5.2 DNS 合成 — 新模块 `network/dns_synth.rs`（或并入 rules.rs）

- 拦截沙箱 `getaddrinfo`（现有 seccomp 通知/`/etc/hosts` 路径扩展）：
  - 解析目标 hostname，若匹配任一 `*.suffix` → 分配合成 IP
    （`127.0.0.2/8` 内，全局唯一）并登记映射；
  - 不匹配 → 现有行为。
- 映射表：`Arc<RwLock<HashMap<synth_ip, String>>>`，容量上限（如 4096），
  超限 LRU 淘汰（防沙箱耗尽 supervisor 内存）。
- 合成 IP 段选择需与 egress-proxy 库错开或统一（避免两套过滤语义冲突）。

### 5.3 `network/connect.rs` + `network/verdict.rs` — 连接判定

- `destination_verdict` 增加 hostname 上下文重载：
  `destination_verdict_with_host(effective, ip, port, hostname)`：
  - 先查合成 IP 映射拿 hostname → 匹配 `wildcard_suffixes`；
  - 命中 → 放行进入代连（supervisor 解析 hostname）；
  - 未命中/无映射 → 走现有 IP/CIDR 判定；
  - 合成 IP 直连且无映射 → 拒绝。
- `connect_on_behalf` 在 `ConnectPlan` 计算处调用新判定；代连目标由
  "已解析 IP" 改为 "实时解析 hostname 的首个 IP"（失败 fail closed）。
- 端口语义：通配规则带端口（`*.example.com:443`）时按端口匹配。

### 5.4 `sandlock-ffi` — 无 ABI 变化

通配条目只是 `net_allow` 字符串，FFI 已透传字符串数组，无需改 C ABI。

### 5.5 Python 绑定（`python/src/sandlock/`）

- `sandbox.py` 字段文档更新（`net_allow` 支持 `*.suffix`）；
- 无参数结构变化；`_sdk.py` builder 已按字符串透传，改动最小。

### 5.6 本项目接入

- `gateway_common/network.py`：`allow_wildcard_domain` 不再依赖
  `egressProxy`（普通模式放开）；denyOut 限制不变。
- `SandlockExecutor`：通配条目直接进 `net_allow`；egress-proxy 库的过滤
  仅保留给 egressProxy 模式。
- 测试：`tests/security/test_network_enforcement.py` 新增通配用例
  （`*.example.com` 子域成功 / 裸域拒绝 / 静态直连 IP 不可绕过）。

## 6. 需要维护的所有部分

### 6.1 fork 仓库

- fork `multikernel/sandlock`；基于上游 tag（当前 0.8.6 或更新）建
  `feature/network-wildcard` 分支；改动以 patch 形式维护，禁止整体改写。
- 上游同步：每次上游发版 rebase 一次，冲突集中在 rules/connect 两文件。

### 6.2 Rust 核心（`sandlock-core`）

- `network/rules.rs`、`network/connect.rs`、`network/verdict.rs`、
  `seccomp/notif.rs`（`NetworkPolicy` 扩展）、新增 `network/dns_synth.rs`；
- 新增单元测试（verdict 纯函数可测）+ `tests/integration/test_network.rs`
  集成用例；
- 关注内存/并发安全（映射表锁、合成 IP 分配）、TOCTOU（沿用
  materialize 先拷贝后判定的既有约束）。

### 6.3 FFI 与 Python 绑定

- `sandlock-ffi`：原则上零改动，但需回归验证；
- `python/src/sandlock/`：文档、类型提示；如需要可在 `_profile.py`
  补充序列化测试。

### 6.4 wheel 构建与发布矩阵

- 构建工具：`setuptools-rust`（仓库自带 `python/setup.py`）；
- 必须产出的 wheel 矩阵（与官方 0.8.6 对齐）：
  - Python：cp310 / cp311 / cp312 / cp313 / cp314；
  - 平台：`manylinux_2_28_x86_64` + `manylinux_2_28_aarch64`；
- 发布方式二选一：
  a. 私有 wheel index（如 `pip index`/`devpi`/OSS 私有桶），Dockerfile 指向；
  b. 镜像构建时从 fork 源码 `pip install git+https://github.com/<org>/sandlock@<rev>`
     （`setuptools-rust` 需在构建镜像内装 Rust 工具链，镜像变大、构建变慢）；
- 建议 a：保持 `Dockerfile.envd` / `Dockerfile.test-runner` 只改一行
  `sandlock==<fork-version>` 来源。

### 6.5 镜像与依赖锁定

- `Dockerfile.envd` / `Dockerfile.test-runner`：sandlock 安装来源改为
  fork wheel/index；
- 锁定 fork 版本（`requirements` 或 pip `==`），升级需重新跑全量；
- 多架构镜像构建（`scripts/build-images.sh`）需在 CI 里对两个架构各编译
  wheel 一次。

### 6.6 测试矩阵

- 单元：`rules.rs` 解析、`verdict` 纯函数、合成 IP 映射并发；
- 集成：`tests/integration/test_network.rs`（真实 sandlock 沙箱）；
- 项目侧：`tests/unit/test_network_config.py`、`tests/security/`
  （通配用例 + 现有 egress-proxy/隔离回归）；
- 平台：x86_64 + arm64 各跑一遍 Linux 全量（Landlock ABI ≥ 6）；
- 回归基线：macOS 227 passed / Linux 249 passed（当前）。

### 6.7 安全面

- 合成 IP 段防绕过（R4）；
- 映射表资源上限（防 OOM）；
- 实时 DNS 解析的 SSRF 面：supervisor 解析 hostname 后仍需经过
  allow/deny（含 CIDR）二次校验，避免"通配规则 + DNS rebinding"绕过；
- 与 egress-proxy 库并存时两套过滤的一致性测试。

### 6.8 上游 PR 与回切

- 把改动整理成面向 `multikernel/sandlock` 的 PR（先 RFC/issue 对齐设计，
  再提代码）；
- 上游合入并发版后：切回官方 wheel，fork 分支归档；回切前跑同一测试矩阵
  确认行为一致。

## 7. 里程碑

| 阶段 | 内容 | 产出 |
|---|---|---|
| M0 | fork + Rust 工具链 + 复现官方 wheel 构建（x86_64） | 可构建的 fork 基线 |
| M1 | R1+R2：规则解析 + DNS 合成 + 映射表 + 单元测试 | `net_allow=["*.example.com:443"]` 可解析、沙箱内返回合成 IP |
| M2 | R3+R4：connect 反查/匹配/实时解析代连 + 防绕过 | 通配子域可连、裸域/直连合成 IP 拒绝 |
| M3 | R5（可选）+ 项目接入（R6）+ 文档 | 普通模式 API 放开、SDK 级用例通过 |
| M4 | wheel 矩阵（cp310-314 × x86_64/aarch64）+ Dockerfile 切源 | 可发布、可部署 |
| M5 | 全量回归（双架构）+ 上游 PR | 与官方对齐、可回切 |

## 8. 备选与渐进路径

- **短期**：维持"egressProxy 模式支持通配、普通模式 400"现状（README 已
  标注差异），不阻塞现有功能；
- **中期**：若不想动 Rust，可把通配域名规则收敛为"HTTP 层规则"
  （`rules`/`http_allow` 通配），但 TCP/非 HTTP 协议仍无法覆盖，不是完整
  一致；
- **长期**（本方案）：sandlock on-behalf 域名规则引擎，完整对齐 E2B。
