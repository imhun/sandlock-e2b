# 会话交接记录（2026-08-29）

> 供新会话快速接续。当前基线：Linux 容器（privileged + host 网络）
> `247 passed, 1 skipped`；macOS `226 passed, 18 skipped`
> （unit + contract + sdk/python + sdk/js + security 跳过项）。

## ⚡ sandlock fork 交接总览（新会话从这里开始）

**位置与分支**：`tmp/sandlock-src`（imhun/sandlock 0.8.6 fork；origin=fork，
upstream=multikernel）。分支：`feature/network-wildcard`（R1–R4 通配规则，
已合入 netns 分支）、`feature/network-netns`（当前主线：netns 可选增强 +
无特权默认网关路径）。

**已完成（Block A 全部）**：

- R1 通配解析（`NetTarget::HostWildcard`，deny 拒绝域名）、R2 合成映射
  （`SyntheticDns`，10.250.0.0/16、LRU 4096）、R3/R4 连接判定 + SSRF 护栏。
- **默认路径（无特权）**：每沙箱 loopback DNS 网关（`127.0.0.x:53`）+
  resolv.conf memfd + connect/send 豁免；netlink 合成视图含虚拟 eth0
  （192.0.2.1/24 + 2001:db8::1/64）修复 glibc AI_ADDRCONFIG。
- **可选隔离增强**：per-sandbox netns（veth + 网关代连），
  `SandboxBuilder::netns(true)` / `E2B_ENABLE_NETNS`，默认关闭。
- UDP 通配（send 路径）、HTTP ACL 代理经网关重定向、FFI/Python `netns`
  绑定、wheel 可构建。

**sandlock 本身状态**：

- **Block B（R8–R11）已完成**（`feature/network-inject`）；**Block C
  （R12–R14）已完成**（`feature/network-socks5`：SOCKS5 on-behalf 替代
  LD_PRELOAD，fail closed，ATYP=domain/IPv4/IPv6，RFC 1929）。
- **上游 PR 已备好**：`upstream-pr/netns-free-clean`（单提交 `d3a28cc`，
  基 `f6a3e39`，无 netns/veth）；**未推送**——当前 `GITHUB_TOKEN` 只读
  （push/API 写均 403），需换写权限 token 或手动推送，见
  `docs/upstream-pr-netns-free.md`。
- **M6 部分完成**：cp311 x86_64 fork wheel 已提交 `wheels/fork/`，
  Dockerfile 按 TARGETARCH 安装；aarch64 wheel 待网络/registry 恢复后构建
  （Docker Hub 在本环境不可达）；cp310/312–314 未做。

**未完成（项目侧 sandlock 落地）**：

- worker/测试镜像的 sandlock 来源切到 fork wheel（当前仍装 PyPI
  `sandlock==0.8.6`；`E2B_ENABLE_NETNS` 与 executor `netns` 参数需 fork
  wheel 才有效，切源前保持默认关闭）。
- security 通配 e2e（fork wheel + privileged worker 下跑
  `tests/security` 新增用例）、迁移（跨 worker netns 重建）验证。

**验证基线（fork，Linux 容器，全程非 root uid=65534）**：lib
`780 passed, 0 failed`（feature/network-socks5；netns-free PR 分支
`770 passed`）；integration `437 passed, 0 failed`（PR 分支 `432`；netns
用例在无 CAP_NET_ADMIN 时按能力跳过）；Python `430 passed, 0 skipped`
（`Dockerfile.test-runner` 已补 `/usr/bin/python3 -> /usr/local/bin/python3`
符号链接）。

**环境注意事项**：

- 容器 `sandlock-dev:latest`（e2b-sandlock-test + rustup/rsproxy +
  iproute2 + **入口脚本**（root 一次：`ip_unprivileged_port_start=0` +
  预置 198.18.0.99–103 回环地址与 /etc/hosts fixture，chmod 共享
  target，然后 `setpriv` 降为 nobody 再执行命令）。宿主
  `~/.cargo/registry` 挂载到 `/opt/cargo/registry` 离线构建。
- **:53 低端口设置的固化**：`net.ipv4.ip_unprivileged_port_start` 是内核
  设置，写不进镜像文件，但可以固化到容器运行时清单——`docker run
  --sysctl net.ipv4.ip_unprivileged_port_start=0`、compose `sysctls:`、
  K8s `securityContext.sysctls`（实测无需 privileged、按容器隔离，容器可
  全程非 root，连入口 root 都不需要）。`--cap-add=NET_BIND_SERVICE` 对非
  root 进程无效（Docker 不注入 ambient caps），`setcap` 文件能力也被
  sandlock 的 no_new_privs 禁用——所以 sysctl 声明是唯一干净的方式。
  **注意**：`--network host` 的容器 Docker 拒绝应用 net sysctl（宿主
  netns 不允许），所以测试容器（host 网络）必须靠入口脚本 root 写一次；
  生产 worker 用桥接网络，compose `sysctls` 生效（docker-compose.prod.yml
  已加）。
- **构建以 root 跑一次**（`--user root --entrypoint bash`，见下），**测试
  全程非 root**——这是 sandlock 无 root 原则的落地；整个套件不再有
  "root 环境性失败"。需要 root 的操作显式
  `--user root --entrypoint bash`。
- netns 集成测试（fork 专属）仍需 `--privileged --network host` +
  `CAP_NET_ADMIN`，非特权环境下自动跳过（`net_admin_available()`）。
- 跑前清 VM 残留（`ip addr del 198.18.0.9x` + 删非 master 的 veth）仅
  root 会话需要。
- 本环境外部 DNS 被透明代理改写为 198.18.x，SSRF 护栏已放行该段；
  e2e 测试用本地 fixture（worker /etc/hosts → 198.18.0.9x）不依赖外网。

**下一步**：① 换写权限 token 推送 `upstream-pr/netns-free-clean` 并开上游
PR；② 构建 aarch64 wheel（网络恢复后）+ cp310/312–314 矩阵；③ 全量双架构
回归 + 生产镜像重建验证。

## 本会话已完成（Block B — header 注入 / maskRequestHost / HTTP 通配）

fork 分支 `feature/network-inject`（基于 feature/network-netns）：

1. **R11 HTTP 通配 matcher**：`HttpRule::matches` 的 host 位置支持
   `*.suffix`（只匹配子域、不匹配裸域、大小写不敏感，与 `net_allow`
   通配语义一致）；`parse` 校验非法形态（`**`/`*.`/`*.*`/内嵌 `*` 拒绝）；
   `extend_net_allow_for_http` 对 `*.suffix` HTTP 规则映射到
   `NetTarget::HostWildcard`（不再当字面 hostname 解析），走 DNS 合成 +
   SSRF 护栏。
2. **R8 credential injection 暴露**：FFI 新增
   `sandlock_sandbox_builder_credential(name, source)` /
   `sandlock_sandbox_builder_http_auth(rule)`；Python 新增
   `Sandbox.http_inject`（list[dict]：matcher/auth/secret/name/on_existing，
   校验 + 序列化）；CLI 沿用既有 `--credential`/`--http-auth`。secret 仍
   只存 supervisor（`SecretString` 零化、env: 变量从子进程剥离）。
3. **R10 host_mask**：`Sandbox`/builder/CLI（`--host-mask`）/FFI/Python
   （`host_mask`）/TOML profile 新增；`transparent_proxy/service.rs` 转发前
   只改写 wire `Host` 头（`${PORT}` 替换为真实目标端口），URI authority
   保持真实（驱动上游连接，hyper-util 保留显式 Host 头）；非法掩码 502
   fail closed。
4. **R9 HTTPS MITM 复用**：注入/掩码在明文与 MITM 共用同一 handler，TLS
   终止路径既有测试覆盖。
5. **验证**：lib 763→771（2 个既有 cow/seccomp root 环境性失败）；integration
   http_acl 16/16（新增 host-mask e2e）；hermetic 代理测试（本地上游断言
   注入头 + 掩码 Host）；Python 全量 412 passed；wheel 可构建且含新符号；
   `Sandbox(http_allow=[...], http_inject=[...], host_mask=...)` 原生构建通过。
   注意：本环境 `target/` 已改为指向 `target-linux` 的符号链接（Python
   `_find_lib` 需要），旧 `target/` 残留已清理。

## 本会话已完成（② 项目切源 + 5B.4 映射 + e2e；③ Block C；④ 上游 PR）

1. **Block C（fork `feature/network-socks5`，e84d65b）**：`network/egress.rs`
   —— SOCKS5 客户端（RFC 1928/1929、poll 驱动、10s 超时、fail closed）；
   `connect_on_behalf` 在 allow/deny 过滤后对所有 TCP 走隧道（通配目标
   ATYP=domain 远程 DNS，字面目标 IPv4/IPv6；UDP/ICMP 直出；loopback remap
   与 DNS 网关豁免）；代理端点由 supervisor 代拨且不进 net_allow（沙箱无法
   直连绕过）；Sandbox/builder/CLI/FFI/Python/profile 暴露 `egress_proxy`
   （含 RFC 1929 凭据，不序列化）。验证：7 单测 + 3 hermetic 集成（隧道/
   fail closed/ATYP=domain）；lib 778、integration 436、Python 414。
2. **② 项目侧**：`gateway_common/network.py` 接受 `maskRequestHost`
   （create-only）与 `rules[].transform.headers`，映射到 `http_inject` /
   `host_mask`；executor 把字面 header 值写入 supervisor-only 0600 文件
   （`E2B_IMAGE_CACHE_DIR/secrets/<sbx>/`），`${e2b.identity.tokens.*}` 映射
   `E2B_IDENTITY_TOKEN_*` env（缺失则创建失败）；修复 chroot 模式下
   `http_inject_ca` 传宿主路径导致 popen 失败的既有 bug（改为沙箱视图路径
   `/home/user/.e2b-ca/...`）；LD_PRELOAD egress 库退役（R14），egressProxy
   统一走 sandlock on-behalf。镜像切 fork wheel（`wheels/fork/`，TARGETARCH
   选择）；requirements-test 不再锁 PyPI 0.8.6。验证：全量 257 passed +
   JS skip；修复过程发现并解决了 SDK 无 `mask_request_host` 字段、
   `api.example.com` 在测试容器不可解析等环境问题。
3. **④ 上游 PR**：`upstream-pr/netns-free-clean` = fork 特性树去掉
   netns/veth（删除 `network/netns.rs`、`netlink/ops.rs`、test_netns、context
   netns pipe、sandbox veth 阶段、`netns` flag/FFI/Python、VethView、
   CLONE_NEWNET），保留无特权 loopback DNS gateway + 虚拟 eth0 +
   wildcard/UDP + Block B + Block C；lib 761、integration 428、Python 412。
   **未推送**（token 只读）；PR 文案见 `docs/upstream-pr-netns-free.md`。

## 本会话已完成（Block A 第一阶段 — sandlock fork：通配域名规则）

1. **fork 基线（M0）**：`tmp/sandlock-src`（imhun/sandlock，0.8.6，
   origin=fork / upstream=multikernel）。构建链：
   `sandlock-dev:latest`（e2b-sandlock-test + rustup/rsproxy）；容器内
   `cargo build --workspace --offline`（宿主 `~/.cargo/registry` 挂载做
   缓存）通过；`sandlock-0.8.6-cp311-cp311-linux_x86_64.whl` 可构建。
   macOS 无法编译 sandlock-core（seccomp/Landlock 仅 Linux），stub
   编译加了非 Linux 宿主宽容（build.rs，Linux 上仍致命）。
2. **R1 通配解析**：`NetTarget::HostWildcard` + 校验（`**`/`*.`/`*.com`/
   `*.*.x` 拒绝）+ `ResolvedNetAllow.wildcard_domains`（不 DNS）；
   `format_net_rule` 往返。
3. **R2 映射表**：`network/dns_synth.rs` —— `SyntheticDns`
   （127.0.0.2/8、双向映射、LRU 4096、耗尽 fail closed）+
   `wildcard_suffix_matches`（子域匹配/裸域不匹配/大小写不敏感）。
4. **R3/R4 连接判定**：`destination_verdict_with_host`；
   `connect_on_behalf` 合成段反查（无映射拒连）→ 实时 DNS 解析改写
   sockaddr 代连 → 解析结果二次 IP 校验。`NetworkPolicy::AllowList` 增
   `wildcard_domains`；`NetworkState` 增 `synthetic_dns`。
5. **测试**：fork 新增 20 用例；`cargo test -p sandlock-core --lib`
   `745 passed`（2 个 cow/seccomp 容器 root 环境性失败，基线一致）。
   fork 改动在 `feature/network-wildcard` 分支。

### 下一步（Block A 未完）

- **M3 项目接入已接线（2026-08-29）**：`gateway_common/network.py` 在
  `E2B_ENABLE_NETNS` 时放开通配 allowOut（否则维持 400）；
  `SandlockExecutor` 增加 `enable_netns`（kwargs `netns`，默认 False 兼容
  旧 wheel）；`docker-compose.prod.yml` worker 加 `NET_ADMIN` +
  `net.ipv4.ip_forward=1` + `E2B_ENABLE_NETNS`；`envd_service/netns.py`
  在 worker 启动时配 ip_forward + veth 网段 MASQUERADE；`Dockerfile.envd`/
  `test-runner` 加 iptables；fork wheel（含 `netns` FFI/Python 绑定）已可
  构建。**待办**：把 worker/测试镜像的 sandlock 来源切到 fork wheel
  （M6 wheel 矩阵/私有源），跑 security 通配 e2e + 全量回归；HTTP ACL +
  netns 组合用例。

## 本会话已完成（Block A 第二阶段 — per-sandbox netns 完整落地）

fork 分支 `feature/network-netns`（基于 feature/network-wildcard）：

1. **netns/veth**：子进程在 userns 之前 `unshare(CLONE_NEWNET)`；父进程
   从全局池（10.200.0.0/16 → /30，沙箱=+2 网关=+1）分配、`IFLA_NET_NS_FD`
   建 veth（修过 VETH_INFO_PEER 嵌套与 `IFLA_NET_NS_FD=28` 常量）、配
   网关端、两端口 UP；子进程经 pipe 收地址后配置自己一端（地址 + 默认
   路由）并回传 ifindex。失败路径删 host 端 veth，teardown 显式删。
2. **DNS 网关**：`gateway:53` UDP listener，通配 A 查询 → `SyntheticDns`
   合成 IP（TTL=0、RA/RD），其余转发 worker 上游；`/etc/resolv.conf`
   memfd 虚拟化；connect 与 send 路径豁免网关端点（glibc res_send 先
   connect UDP socket——connect 不豁免则 res_query 直接 -1，排查最久的坑）。
3. **netlink 视图**：`NetlinkState` 增 `VethView`（子进程回传 ifindex），
   GETLINK/GETADDR dump 加入 veth（否则 glibc AI_ADDRCONFIG 只见回环，
   getaddrinfo 不发 DNS 直接 -3）。
4. **HTTP 代理**：`spawn_transparent_proxy` 增 `bind_ip`，netns 模式绑
   网关地址（代理创建移到 veth 建立之后）。
5. **SSRF 护栏**：通配解析后的真实 IP 拒绝私网/回环/链路本地/CGNAT/ULA/
   组播（放行 198.18/15 与 TEST-NET，防透明代理/拦截 DNS 误伤）；二次
   校验带 hostname 上下文（AllowList 下真实 IP 才能通过通配规则）。
6. **测试**：lib `762 passed`（2 个既有 cow::seccomp root 环境性失败）；
   integration `428 passed`（1 个事务合并 root 环境性失败）。新增
   `test_netns.rs` 三用例全绿：loopback 隔离 / 通配 DNS 合成 IP /
   通配 connect 到真实目标。
7. **补充（fork abe7bf8）**：UDP 通配落地——sendto/sendmsg/sendmmsg 对
   合成 IP 反查 hostname、通配判定、实时解析 + SSRF 护栏、改写 sockaddr
   后代连（QUIC 等 UDP 通配可用）；`check_ip_destination` 被新的
   `resolve_send_destination` 取代。`test_netns.rs` 扩为 5 用例全绿：
   loopback 隔离 / 通配 DNS 合成 IP / 通配 TCP connect（本地 fixture，
   不依赖外部 DNS）/ 通配 UDP 到达真实目标 / HTTP ACL 经网关代理重定向。
   全部改 multi_thread runtime（current-thread 会饿死 supervisor/DNS 任务
   导致 create 挂起）+ 30s 快速失败超时。全量：lib 762 passed（2 既有
   root 环境性失败）、integration 430 passed（1 既有 root 环境性失败）、
   wheel 可构建、Python `Sandbox(netns=True, net_allow=["*.example.com:443"])`
   可用。
8. **并行安全（fork 8709846）**：`WorkerLocalHost` 改为每实例独立
   `198.18.0.x` 地址（首个空闲，进程级互斥锁保护地址与 `/etc/hosts`
   读写，Drop 只删自己的行/地址），三个本地服务器用例绑定实例地址。
   netns 套件在**默认并行**下 5/5 通过（0.2s），不再需要
   `--test-threads=1`。注意：既有 `test_control` 族在并行下随机互踩
   （每次失败成员不同、单独跑都过），与 netns 无关；并行全量基线
   429+2（control 随机 + txn root 环境性），串行基线 430+1。
9. **无特权默认路径（fork 89b31d9）**：通配运行时改回共享 netns +
   loopback DNS 网关（每沙箱绑 `127.0.0.x:53`，resolv.conf 不支持端口故
   每沙箱独立 loopback 地址）；合成 IP 段迁到 `10.250.0.0/16`（与网关
   段彻底分开，connect 豁免优先于合成反查）；netlink 合成视图新增固定
   文档地址虚拟 `eth0`（192.0.2.1/24 + 2001:db8::1/64）让 glibc
   `__check_pf`/AI_ADDRCONFIG 看到非 loopback 族（顺带修复无特权实时
   DNS 基线问题）。**默认路径完全无特权**；per-sandbox netns（veth +
   loopback 隔离）保留为 `netns(true)` / `E2B_ENABLE_NETNS` 可选增强。
   项目侧 wildcard allowOut 默认放行（不再依赖 egressProxy 或
   E2B_ENABLE_NETNS）。验证（历史基线）：lib 763+2、integration 432+1
   （root 环境性，后已改为全程非 root 全绿，见"验证基线"）。
   Block B/C 在同一无特权 seccomp/loopback 模型上实现。

环境注意：`sandlock-dev:latest` 已加 iproute2；集成测试需
`--privileged --network host`；e2e 连接用例临时改容器 resolv.conf 为
8.8.8.8 并配 ip_forward + MASQUERADE（本环境 DNS 被透明代理改写为
198.18.x，护栏已放行）。

## 本会话已完成（Network API 阶段 B1 — egressProxy）

1. **LD_PRELOAD SOCKS5 隧道库** `envd_service/egress/libegress_proxy.c`：
   hook `getaddrinfo`（域名→合成 127.0.0.2/8 + hostname 映射，沙箱内不发
   DNS）与 `connect`（恢复 hostname/直连 IP → 库内 allowOut/denyOut 过滤 →
   SOCKS5 RFC1928/1929 握手，域名走 ATYP=domain 远程 DNS；非阻塞 fd 同步
   等待连接完成；代理不可达/握手失败 → ECONNREFUSED，fail closed）。
2. **执行器集成**：`egressProxy` 模式下 net_allow 只放行代理端点，库经
   LD_PRELOAD 注入（chroot 模式复制进 workspace/.egress 并以
   /home/user/.egress 路径加载），EGRESS_PROXY/ALLOW/DENY/USER/PASS 走
   env；库源码由 worker 首次使用时 `cc` 构建并缓存到
   `E2B_IMAGE_CACHE_DIR/egress/`。动态更新复用 `update_network`。
3. **控制面校验**：`egressProxy.address` 必须解析到公网 IPv4（拒绝
   私网/loopback/link-local，防 SSRF），username/password ≤255；update 中
   `egressProxy: null` 显式清除。create/update/detail 全链路。
4. **测试**：安全用例 2 个（隧道 + ATYP=domain 断言、deny 拦截），单元
   校验用例；测试镜像加 `gcc`/`libc6-dev`。
5. **限制**：仅 IPv4 代理；仅动态链接应用（python/node）；过滤在沙箱内
   库做（LD_PRELOAD 方案固有妥协）；rules/maskRequestHost 仍 400。

## 本会话已完成（最终容器镜像 + 分离部署）

1. **镜像分离**：`Dockerfile.control-plane` 只含 `gateway_common` +
   `control_plane`；`Dockerfile.envd` 只含 `gateway_common` + `envd_service`，
   且 multi-stage 预编译 `libegress_proxy.so` 到 `/opt/egress/`（最终镜像
   不带 gcc）。代码层解耦：env 工具函数移到 `gateway_common/env.py`；
   控制面 `create_app` 对 `RuntimeRegistry` 懒导入，分离模式用 no-op
   哨兵（pause/resume/snapshots/kill 等调用安全）。
2. **构建脚本** `scripts/build-images.sh`：buildx 多架构
   （`linux/amd64,linux/arm64`），多平台需 `PUSH=1`。
3. **部署示例** `docker-compose.prod.yml` + `.env.example`：控制面 +
   gateway + worker-1/2/3（YAML anchor）+ Redis（共享状态）+ 可选本地
   registry（profile）；`docker-compose.yml` 单机示例控制面改为
   `E2B_ENABLE_LOCAL_NODE=false`。
4. **验证**：两镜像构建成功（镜像内容分离确认）；`compose config` 有效；
   macOS 起栈（`--no-build` 强制用分离镜像）三 worker 验证全绿：
   `multinode_smoke.py`（跨节点分布覆盖 3 worker/命令/文件/stdin/配额释放）
   + `scripts/deployment_smoke.py`（追加迁移 worker-2→worker-1 共享
   workspace 文件保留、network 回显/原子更新）。踩坑记录：宿主 3000 端口
   被占用需换端口；本机 docker daemon 里 `python:3.11-slim` 曾被 arm64
   spike 覆盖导致沙箱 qemu-arm64——拉回 amd64 后正常（顺带验证了 rootfs
   digest 缓存失效）。

## 本会话已完成（Network API，阶段 A + C）

1. **network 配置全链路**：`POST /sandboxes` 的 `network` 字段与
   `PUT /sandboxes/{id}/network`（官方 `update_network`，原子替换、省略字段
   清空、`allowPublicTraffic` 仅创建时可设）。wire 格式为 camelCase
   （`allowOut`/`denyOut`/`allowPublicTraffic`/`rules`；更新体 `allow_internet_access`
   兼容 SDK 的 snake_case 拼写）。`SandboxRecord`/`RuntimeSandbox` 新增
   `network` 字段并持久化（Redis 可见），`as_detail` 回显。
2. **运行时映射（阶段 A）**：`allowOut`/`denyOut`/`allowInternetAccess` →
   Sandlock `net_allow`/`net_deny`（互斥：两者都在时 allowlist 模型胜出、
   deny CIDR 覆盖的 allow 条目被剔除）；`rules` 域名 → `http_allow`
   （80/443 透明 MITM ACL，镜像 rootfs 模式把临时 CA 拼进每沙箱信任副本并
   注入 `SSL_CERT_FILE`/`CURL_CA_BUNDLE`）。`LocalExecutor` no-op。
3. **动态更新**：控制面保存后 `_push_network_config` 推送到节点 agent
   （`POST /agent/sandboxes/{id}/network`），agent 更新 `RuntimeSandbox` 并
   调用 `SandboxRuntimeContext.update_network`；RPC `_context` 增加网络配置
   drift 检测，下条命令用新策略。
4. **allowPublicTraffic**：envd HTTP/Connect 鉴权在
   `runtime.allow_public_traffic` 时跳过 token 校验（仍校验 sandbox id）。
5. **显式拒绝（no fake success）**：`egressProxy`、`maskRequestHost`、
   `rules.transform`（header 改写）返回 400，标注依赖阶段 B 代理层。
6. **测试**：单元（校验/映射/序列化/executor kwargs）14 个；契约 5 个
   （回显/原子更新/404/拒绝/allowPublicTraffic/SDK 往返）；Sandlock 强制
   1 个（deny 后 update_network 恢复 egress，`example.com:443` 实测 200）。
   注意：多节点 harness worker 现设 `enable_network=True`（默认 false 时
   网络策略不生效）。

## 本会话已完成

1. **P0 并发迁移锁**：`SandboxRegistry.try_acquire_migration/release_migration`
   —— Redis 多副本用 `SETNX` 标记 + TTL（WATCH 对比删除，兼容 fakeredis），
   单进程用等价内存锁；并发 migrate 第二个请求返回 409；失败路径
   `finally` 释放标记。单元测试覆盖跨副本互斥、过期释放、错误 token 不能
   释放；契约测试覆盖持锁 409 + 释放后可迁移。
2. **P1 双活窗口关闭**：迁移先调源节点 `DELETE ?keepFiles=true`（停
   runtime、杀进程树、保留文件）再导出/导入/切路由；失败时自动在源节点
   重新 provision（`_provision_local` 现支持幂等替换过期挂载符号链接），
   并回滚已持久化的 node_id。契约测试：目标 provision 失败（指向控制面
   自身地址 → 快速 404）后沙箱命令仍可用。
3. **P1 rootfs 缓存失效**：`resolve_image_rootfs` 缓存目录名加入镜像
   digest（`docker image inspect` 的 RepoDigests/Id），tag 更新自动换新
   rootfs；先 pull + login 再算 digest，避免首次解析重复解包。README 补充
   说明与手动清理方式。
4. **P2 JS SDK 测试在宿主跑通**：`pytest tests/sdk/js`（npm 在宿主机），
   vitest 全量通过；README 测试表更新为 macOS / Linux 均可。

## 本会话验证结果

```text
macOS: 200 passed, 6 skipped（tests/unit + tests/contract + tests/sdk/python + tests/sdk/js）
Linux: 225 passed, 1 skipped（全量含 Sandlock/registry/真实 Redis/模板隔离）
```

## 本会话已完成

1. **Template COPY 文件上下文**：`GET /templates/{id}/files/{hash}`（201）返回
   带 token 的上传 URL，`PUT .../upload` 校验 token 并存储归档；构建时解包进
   `ctx/` 作为 docker build context；COPY 步骤生成 Dockerfile
   （`--chown/--chmod`）；构建先 push 成功才置 ready。
2. **真实 Redis 多副本**：`SandboxRegistry.save()` 持久化 node_id/TTL/暂停等
   变更；Redis 模式下 `get/list` 总读共享存储；`E2B_REDIS_URL` 生效；
   `tests/contract/test_redis_multireplica_e2e.py` 用真实 redis（容器内
   `redis-server` 进程，macOS 回退 docker redis:7）。
3. **节点故障迁移**：`POST /sandboxes/{id}/migrate`（可选 `nodeID`）——
   agent `export/import`（tar.gz）、配额转移、源清理、gateway 路由失效
   （`E2B_GATEWAY_URL`）；非共享卷按卷亲和约束。
4. **命令输出日志**：`command-logs.jsonl` 落盘（stdout/stderr/PTY、ANSI
   剥离、1MB/命令 + 16MB/文件截断），`GET /sandboxes/{id}/logs` 合并返回；
   远程经 `GET /agent/sandboxes/{id}/logs` 拉取。
5. **共享 workspace 模式**：`E2B_SHARED_WORKSPACE_ROOT` 时迁移只切路由
   （跳过 export/import），源目录不删（`DELETE /agent/sandboxes/{id}
   ?keepFiles=true` 仅释放 runtime）。
6. **镜像仓库分发**：`E2B_IMAGE_REGISTRY` 构建后 tag+push，模板镜像名切到
   `{registry}/{templateID}`；worker resolver 本地无镜像先 `docker pull`。
7. **镜像仓库认证**：`E2B_IMAGE_REGISTRY_USERNAME/PASSWORD`（控制面+worker），
   `docker login --password-stdin`（密码不进 argv）。

## 未完成 / 待办（按优先级）

### P2 — 真实 NFS 部署未验证

共享 workspace/volume 目前只在同一主机共享目录模拟；NFS/CSI 上的
root_squash、uid=1000 映射、命令 IO 延迟未实测。部署验证时注意
`E2B_SHARED_VOLUME_ROOT` / `E2B_SHARED_WORKSPACE_ROOT` 各节点路径语义一致。

### P3 — 遗留优化 / 后续 Block（sandlock fork）

- 迁移导出 tar 仍含卷挂载符号链接空条目（功能等价，可显式排除）；
- 未配置 `E2B_GATEWAY_URL` 时迁移后路由依赖 gateway 30s 缓存 TTL（文档已知）；
- ~~**Block B — header 改写（rules.transform / maskRequestHost）**~~：fork
  `feature/network-inject` 已完成（R8–R11，见上）；剩项目侧 5B.4 映射
  （依赖 fork wheel 切源后生效）。
- **Block C — SOCKS5 on-behalf**：`ConnectPlan::Socks5Upstream` 替代
  LD_PRELOAD egress 库（R12–R14），纯 TCP 握手无特权可实现。
- **M6 — wheel 矩阵**：cp310–314 × x86_64/aarch64 + 私有 index / git 安装
  切换（worker 与测试镜像当前仍装 PyPI 0.8.6）。
- **M7 — 上游 PR**：把无特权部分整理成面向 `multikernel/sandlock` 的 PR；
  netns 留 fork 分支（上游是无特权项目，netns 特权要求难被接受）。
- **LD_PRELOAD 隧道已知限制**：静态/Go 应用不受影响（可后续用 sandlock
  on-behalf connect 的 SOCKS5 分支替代，语义更完整）；IPv6 目标/代理未
  隧道（直接 real connect）。
- spec.md 其余官方 API 面（iam/lifecycle 等）仍未支持，入口处
  `UNSUPPORTED_FIELDS`/`UNSUPPORTED_ENDPOINTS` 明确拒绝。

## 验证命令与基线

```bash
# macOS（端口绑定需提权）
tmp/venv/bin/python -m pytest tests/unit tests/contract tests/sdk/python \
  -q -p no:cacheprovider

# Linux 容器全量（含真实 Redis / registry 认证 / Sandlock 用例）
docker run --rm --privileged --network host \
  -e E2B_BASE_IMAGE=python:3.11-slim \
  -e E2B_HOST_PROJECT="$(pwd)" \
  -v ~/.orbstack/run/docker.sock:/var/run/docker.sock \
  -v "$(pwd):/workspace" -w /workspace \
  e2b-sandlock-test:latest pytest tests --perf -q -p no:cacheprovider
```

- 测试镜像 `e2b-sandlock-test:latest`（Dockerfile.test-runner，国内源，
  已含 redis-server）；改依赖后需重建。
- `--network host` + `E2B_HOST_PROJECT` 是容器内 docker CLI 访问宿主
  localhost 端口 / 挂载宿主路径的前提（registry/Redis 端口映射、htpasswd
  挂载）。
- 多节点冒烟：`scripts/multinode_smoke.py` + `scripts/deployment_smoke.py`
  （后者含迁移/共享 workspace/network 更新）；compose：
  `docker compose -f docker-compose.multinode.yml up -d`。

### sandlock fork 验证（Linux 容器）

```bash
# 1) 一次性 root 构建（FFI/测试二进制；入口脚本会 chmod 共享 target）
docker run --rm --privileged --network host --user root --entrypoint bash \
  -v "$(pwd)/tmp/sandlock-src:/src" \
  -v ~/.cargo/registry:/opt/cargo/registry \
  -v "$(pwd)/tmp/sandlock-dev/cargo-config.toml:/opt/cargo/config.toml" \
  -w /src sandlock-dev:latest -c '
    cd /src && CARGO_TARGET_DIR=/src/target-linux cargo build -p sandlock-ffi --offline
    chmod -R a+rwX /src/target-linux'

# 2) 全程非 root 测试（入口 root 准备后自动降权 nobody；命令用 bash -c，
#    不要 bash -lc —— login shell 会重置 PATH）
docker run --rm --privileged --network host \
  -v "$(pwd)/tmp/sandlock-src:/src" \
  -v ~/.cargo/registry:/opt/cargo/registry \
  -v "$(pwd)/tmp/sandlock-dev/cargo-config.toml:/opt/cargo/config.toml" \
  -w /src sandlock-dev:latest bash -c '
    cd /src && CARGO_TARGET_DIR=/src/target-linux \
    cargo test -p sandlock-core --offline --lib
    cargo test -p sandlock-core --offline --test integration -- --test-threads=1
    cd python && PYTHONPATH=/src/python/src python -m pytest tests -q -p no:cacheprovider'

# netns 套件（需要 CAP_NET_ADMIN；非特权环境自动跳过）
cargo test -p sandlock-core --offline --test integration test_netns -- --test-threads=1
```

## 关键文件索引

| 文件 | 内容 |
|------|------|
| `control_plane/api/sandboxes.py` | migrate（per-sandbox 锁 + 先停源 runtime + 失败回滚）、network 创建/`PUT /sandboxes/{id}/network`/`_push_network_config`、logs 合并、keep_files 销毁 |
| `control_plane/registry/manager.py` | Redis save/get/list、TTL 回收、`try_acquire_migration`/`release_migration`（SETNX + TTL / 内存锁） |
| `control_plane/api/templates.py` | COPY 上传链路、registry push/login |
| `control_plane/registry/nodes.py` | `select_and_reserve(exclude_node_id)`、`reserve_node` |
| `envd_service/agent.py` | export/import/logs/keepFiles 端点、`POST /agent/sandboxes/{id}/network` 更新端点 |
| `envd_service/process/logs.py` | 命令输出 JSONL 采集 |
| `envd_service/runtime/image_resolver.py` | rootfs 解包、pull、registry login、digest 缓存 key |
| `envd_service/gateway.py` | 路由缓存 + `/internal/routes/{id}/invalidate` |
| `gateway_common/network.py` | network 校验/规范化 + sandlock 策略映射 |
| `envd_service/executors/sandlock.py` | network→net_allow/net_deny/http_allow + 每沙箱 CA 注入 |
| `envd_service/egress/libegress_proxy.c` | LD_PRELOAD SOCKS5 隧道库（getaddrinfo/connect hook + 过滤 + ATYP=domain） |
| `envd_service/egress/build.sh` | 库构建脚本（gcc） |
| `envd_service/runtime/context.py` | `update_network` + RPC drift 检测 |
| `tests/contract/test_network_api.py` | network 契约（回显/更新/拒绝/allowPublicTraffic） |
| `tests/security/test_network_enforcement.py` | deny→update→allow 强制用例 |
| `tests/security/test_egress_proxy.py` | SOCKS5 隧道 + 远程 DNS + deny 拦截用例 |
| `tests/unit/test_network_config.py` | network 校验 + sandlock 映射单测 |
| `tests/conftest.py` | live/multinode/registry/redis fixtures（session 级） |
| `tests/contract/test_migration.py` | 迁移 + 共享 workspace + 持锁 409 + 失败回滚用例 |
| `tests/contract/test_redis_multireplica_e2e.py` | 真实 Redis 多副本 |
| `tests/contract/test_command_logs.py` | 命令日志合并（本地 + 远程） |
| `tests/contract/test_template_upload.py` | COPY 上传契约 |
| `tests/sdk/python/test_templates.py` | 构建、COPY、registry push/pull/认证 |
| `tests/unit/test_sandbox_registry.py` / `test_redis_multireplica.py` | 迁移锁单元测试（内存 + fakeredis） |
| `Dockerfile.control-plane` / `Dockerfile.envd` | 分离的最终镜像（envd multi-stage 预编译 egress 库，最终镜像无 gcc） |
| `docker-compose.prod.yml` / `.env.example` | 生产部署示例（控制面+gateway+worker+Redis+可选 registry） |
| `scripts/build-images.sh` | buildx 多架构（amd64/arm64）镜像构建脚本 |
| `gateway_common/env.py` | env 工具函数（消除 control_plane↔envd_service 交叉导入） |

## 配置速查（新增项）

```text
E2B_SHARED_WORKSPACE_ROOT      共享工作目录（迁移只切路由）
E2B_IMAGE_REGISTRY             模板镜像 push 目标
E2B_IMAGE_REGISTRY_USERNAME    仓库认证（控制面 push / worker pull）
E2B_IMAGE_REGISTRY_PASSWORD    仓库认证
E2B_GATEWAY_URL                迁移后通知 gateway 失效路由
```
