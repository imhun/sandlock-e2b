# 会话交接记录（2026-08-29）

> 供新会话快速接续。当前基线：Linux 容器（privileged + host 网络）
> `247 passed, 1 skipped`；macOS `226 passed, 18 skipped`
> （unit + contract + sdk/python + sdk/js + security 跳过项）。

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

### P3 — 遗留优化

- 迁移导出 tar 仍含卷挂载符号链接空条目（功能等价，可显式排除）；
- 未配置 `E2B_GATEWAY_URL` 时迁移后路由依赖 gateway 30s 缓存 TTL（文档已知）；
- **B2 — header 改写（rules.transform / maskRequestHost）**：sandlock 上游
  main 分支已有 credential injection（`InjectRule`/`AuthShape`，透明代理内
  header 注入），但 PyPI 0.8.6 未发版。接入方式：等上游发版，或 fork
  sandlock 把 inject 暴露到 Python 绑定（`credential.rs`/ffi 已就绪）。
  `allowOut` 通配域名（`*.example.com`）已在 egressProxy 模式下支持
  （库内 `*.suffix` 匹配：匹配子域、不匹配裸域名），普通模式仍 400。
- **普通模式通配域名（与 E2B 标准一致）**：完整需求点/方案/维护面见
  `docs/sandlock-network-wildcard.md`——需要 fork sandlock 在 on-behalf
  connect 路径加域名规则引擎（合成 IP 映射 + hostname 反查匹配），
  不能走 LD_PRELOAD（安全降级）。**fork 载体已确定：
  `https://github.com/imhun/sandlock`（默认分支 main，已验证可克隆）**，
  改动在其 `feature/*` 分支上进行，上游 `multikernel/sandlock` 作为
  upstream 定期同步。
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
