# 安全加固：P0-P2 修复记录

2026-08-31 对远程部署实例做安全实测后实施的加固，含验证结果与配置说明。

## 实测发现的漏洞与结论

| 项 | 实测结果 | 状态 |
|---|---|---|
| 内网横向访问 | 全放行网络沙箱可直连 redis（无密码可读写）/buildkit（无认证）/control-plane/worker；默认白名单沙箱全部拒绝 | 已修复（P0） |
| 磁盘无配额 | 1.5G 写入无运行时限制 | 已缓解（P1，见下） |
| 跨沙箱 kill | 同 worker 目标可见但 `os.kill` 返回 EPERM（sandlock 拦截） | 已确认防护有效 |
| PTY 跨沙箱 | `/dev/pts` 无从端节点，扫描不可达 | 已确认防护有效 |
| 通知洪泛 | 400 次 on-behalf connect 对 API 延迟无明显影响 | 增加限流（P1） |
| UDP 出站 | 全放行沙箱 UDP 出站不受限 | 随 P0 私网拒绝覆盖 |

## 修复内容

### P0-1 网络策略：默认全放行改为"公网放行 + 私网拒绝"

- `gateway_common/network.py`：`sandlock_network_policy` 新增
  `private_deny_cidrs` 参数。无显式 `allowOut`/`denyOut` 且允许联网时，
  不再生成 `net_allow=["*:*"]`，而是生成 `net_deny=<私网段>`（sandlock
  DenyList：default-allow 公网、拒绝私网）。
- **显式 `allowOut` 不受影响**：用户明确放行内网 IP/CIDR 时照常工作，
  不做私网过滤。
- worker 配置 `E2B_NETWORK_DENY_CIDRS`（默认 RFC1918 + loopback +
  link-local + ULA）。需要放行内网服务时从该列表裁剪对应段；
  置空则完全关闭保护。

### P0-2 redis 认证

- compose redis 增加 `--requirepass ${E2B_REDIS_PASSWORD}`，healthcheck 带
  密码；`E2B_REDIS_URL` 改为 `redis://:<password>@redis:6379/0`。
- `upgrade.sh` 首次部署自动生成并保留 `E2B_REDIS_PASSWORD`。

### P0-3 buildkit 去裸 TCP

- `buildkitd.toml` 监听 unix socket（`unix:///run/buildkit/buildkitd.sock`），
  buildkit 与控制面通过共享卷 `buildkit-sock` 通信，不再暴露 TCP 端口。

### P1-4 磁盘配额

- executor 透传 `max_disk` 到 sandlock（COW 模式下生效；当前共享 workspace
  非 COW 形态下 sandlock 的磁盘配额不生效，实际限制仍需依赖控制面准入 +
  worker 磁盘水位监控/告警）。

### P1-5 seccomp 通知限流（sandlock fork）

- sandlock 新增 `notify_rate_limit`（builder / FFI / Python / CLI）：
  每秒最多处理 N 个 seccomp 通知，超限 supervisor 睡满窗口剩余时间，
  沙箱被拦截的 syscall 在内核队列积压/阻塞，防止通知洪泛压垮 supervisor。
- worker 配置 `E2B_SANDBOX_NOTIFY_RATE_LIMIT`（默认 5000/s，0 关闭）。
- 需重新构建 wheel 并发布镜像后生效（见部署步骤）。

### P2-7 创建限流

- 控制面 `POST /sandboxes` 按 API key 滑动窗口限流（默认 120/min/key，
  `E2B_CREATE_RATE_LIMIT_PER_MIN=0` 关闭），防 `X-Sandbox-Id` 幂等创建轰炸。

### P2-6 PID namespace（未实现，见说明）

- 跨沙箱信号已被 sandlock seccomp 拦截（EPERM），当前主要残余是 PID
  存在性可探测（信息泄露）。完全隔离需要 sandlock fork 内两级 fork +
  `CLONE_NEWPID`，会触及 pidfd / process_vm_readv / 进程组管理等核心机制，
  且需 Linux 全量回归；本次未实施，列为后续深度改造项。

## 部署步骤

1. 重新构建 sandlock wheel：`./deploy/scripts/build-sandlock-wheels.sh`
2. 构建镜像并推送：`./deploy/scripts/build-and-push.sh`
3. 部署：`./deploy/scripts/upgrade.sh`（首次会自动生成 `E2B_REDIS_PASSWORD`；
   现有部署改 `.env` 后 `--force-env` 或手动补 key 再升级）

## 复测

- 内网隔离：`tmp/security-probe2.py`（OPEN 沙箱应全部拒绝内网 TCP）
- 限流/PTY/kill：`tmp/security-probe3.py`
- 单测：`pytest tests/unit/test_network_config.py tests/unit/test_ratelimit.py`

---

# 后续评估：未解决的安全问题（2026-09-01）

> 状态提醒：上文 P0-P2 修复**尚未部署**，生产仍为旧镜像，实测漏洞（内网
> 横向访问、redis 无密码、buildkit 裸 TCP 等）在生产上依然存在。部署
> （构建 + `upgrade.sh`）是所有后续项的前置。

## 1. 共用 uid：文件系统隔离单点边界（中风险）

### 现状与问题

所有沙箱 `RunAs` uid 1000（sandlock 单 entry userns 只能映射一个 uid/gid，
无 supplementary groups）。后果：

- 沙箱目录即使 `0700`，**同 uid 下内核权限失效**（同 uid 即所有者）；
- 沙箱间文件隔离**只剩 Landlock 一条防线**：`fs_writable/fs_readable/fs_denied`
  规则正确则隔离成立，Landlock 有洞/被绕过则**所有沙箱文件全开**；
- 跨沙箱信号已隔离（每实例独立 userns，实测 EPERM），但 userns 不隔离
  文件权限（按映射后的 host uid 判定）；
- unix socket / IPC 无显式隔离（Landlock 不管，同 uid 权限不设防）。

### 修复：每沙箱独立 uid（sandlock `RunAs` 已支持）

每沙箱分配 host uid（如 10000+i）。收益：

- Landlock 失效时的内核权限兜底（**`0770` owner=沙箱 uid、group=worker gid** + 独立 uid = 真隔离：
  沙箱 `setgroups([])` 且 gid=自己，不在 worker 组，所以 other 位为 0 的 `0770` 对它等价于 `0700`；
  worker 作为数据面所有者走属组读写，详见 `production-deployment-requirements.md` §2.4 的 c1 段）；
- unix socket / IPC 随 uid 隔离（额外收获）；
- 与 usrquota 路线兼容（如需 per-uid 配额）。

代价（必须配套设计）：

- **volume 共享权限模型**：volume 跨沙箱共享，属主是创建者 uid；其他沙箱
  （不同 uid）挂载后无法读写。单 entry userns 不能给 supplementary group，
  需 0777 + sticky 或 volume 属主策略；
- **镜像 rootfs 属主**：rootfs 解压属主通常 1000，换 uid 后 chroot 内
  `/home/user` 等挂载点需由 workspace 目录覆盖属主；
- **uid 生命周期**：worker 分配/回收、`sandbox.json` 持久化、孤儿清理。

## 1b. 共享 PID namespace（低-中风险，P2-6 遗留）

### 现状

所有沙箱共享 worker 的 PID namespace（`CLONE_NEWPID` 未使用，仅在
`sys/structs.rs` 定义常量）。已缓解层：

- 信号攻击被每实例独立 userns 挡住（实测 EPERM）；
- `/proc` 被 sandlock 虚拟化（procfs.rs 拦截敏感路径、过滤 PID，实测
  `/proc/1/status` 读取被拒）；
- ptrace 被 seccomp gate。

### 残留风险

- **PID 存在性探测**：`kill(pid, 0)` 返回 EPERM（存在）vs ProcessLookupError
  （不存在）可枚举存活 PID——信息泄露；
- **纵深叠加缺口**：同 uid + 共享 PID ns + 共享 netns，进程/文件/网络三个
  维度都依赖运行时规则（seccomp/userns/Landlock）而非内核命名空间隔离，
  任一被绕过则无兜底。

### 修复

`CLONE_NEWPID`（fork 结构改造：child 内 unshare + 再 fork，或改用
`clone(CLONE_NEWPID)`），涉及 pidfd/process_vm_readv/进程组管理的 pid 引用，
需 Linux 全量回归。当前攻击面已被 seccomp/userns/procfs 虚拟化覆盖，
优先级低于租户隔离与独立 uid。

## 2. 控制面 API 无 TLS（P0，实测确认）

`https://172.18.78.49:3000` 不可用，`http://` 正常——API key、volume token、
internal key、沙箱数据**明文**经公网代理传输。修复依赖代理层 TLS 或控制面
HTTPS。

## 3. 无租户隔离（架构级，高）

所有 API key 权限相同：持有任一 key 可列/删所有沙箱与 volume、读快照/模板、
挂载任意 volume 读内容。多租户场景需 per-key 资源归属/授权模型（与独立 uid
的"资源归属"一起设计）。

## 4. token 无过期/失效（中）

- **volume token**：`token_hex(24)` 强随机但永久有效、无吊销（泄露一次 =
  永久读写该 volume）；
- **template upload token**：`token_urlsafe(32)` 强随机，`mark_file_uploaded`
  不清除 token，上传后可重复 PUT 覆盖 build context。

## 5. 模板构建无资源限额（中）

`/v3/templates` 构建仅 API key 鉴权，无并发/资源限流（走 buildkit，消耗
CPU/磁盘）。恶意 key 可并发构建轰炸（`/sandboxes` 已限流，模板/快照/volume
未限）。

## 6. internal key 共享且无轮换（低-中）

所有 worker 共用单 `X-Internal-Key`，无轮换机制；默认值 `"internal-key"`
弱（生产已覆盖，配置漂移风险）。

## 7. secret 明文驻留与凭据落盘（低）

- `SecretRegistry` value 明文存控制面内存，无持久化（重启丢失）、无加密；
- 已修复（E5.4）：配置 `E2B_SECRET_MASTER_KEY` 后 secret 以 Fernet
  （AES）加密落盘并镜像到 Redis，重启不丢；轮换走
  `E2B_SECRET_MASTER_KEYS` 双 key 窗口（`upgrade.sh
  --rotate-secret-master-key` / `--finalize-secret-master-key-rotation`）。
  未配置 master key 时保持降级（内存 + 明文盘）并启动告警。
- `.env`（ACR 密码/API key/redis 密码/secret master key）、`bastion.env`
  （SSH 口令）明文落盘（权限 600）；生产建议 master key 与 ACR 口令上
  密钥管理（O3：Secret Manager / KMS，启动时注入环境变量，避免 `.env`
  明文长期驻留）。

## 8. 内存 DoS 与进程权限（新增，2026-09-01 二轮评估）

### 8.1 命令输出缓存无上限（高）

`ProcessManager.captured`（`dict[str, bytearray]`）缓存命令全部输出，
`buf.extend(chunk)` 无限增长——沙箱内 `cat /dev/zero` 等可打爆 worker
内存（3600s 超时前即可耗尽）。修复：capped buffer（如 10MB，超出截断）。

### 8.2 文件写入 API body 无大小限制（高）

- `/volumecontent/{id}/file` PUT、template upload、worker `files.write`
  均为 `await request.body()` 整读入内存再写盘，FastAPI 无默认上限；
- 上传超大文件 = 控制面/worker 内存 DoS；
- 修复：流式写盘 + `Content-Length`/分块大小限制。

### 8.3 worker/supervisor 以 root 运行（高，逃逸放大）

`Dockerfile.envd` 无 `USER`——worker 容器以 root 跑，sandlock supervisor
是 root 进程。Landlock/seccomp 被完全绕过时攻击者获得容器 root。
sandlock 官方支持非 root 运行（uid 65534 全绿）——worker 应以非 root 用户
跑 supervisor，降低逃逸后果。

### 8.4 依赖未锁定（中，供应链）

`fastapi>=0.110` 等核心依赖全部 `>=`，生产构建不可复现。修复：锁版本
（`==` 或 lockfile）。

### 8.5 元数据/产物无大小限制（中）

- 创建沙箱只校验 metadata/envVars 类型不限制大小 → `sandbox.json` 膨胀；
- 模板/快照/构建产物无大小限制（与 8.2 同源）。

### 8.6 低风险项（2026-09-01 三评估）

- **节点失联僵尸沙箱**（中低）：unhealthy 节点不清理沙箱；网络分区窗口内
  worker 沙箱继续跑、控制面 TTL 删目录 → 进程持有已删 inode + 恢复后记录
  不一致。已修复（E6.1）：控制面周期扫描把失联节点沙箱标记为
  `orphaned`（TTL 跳过，不再删运行中沙箱的 workspace）；worker 恢复后
  通过 `POST /internal/nodes/{id}/reconcile` 双向对账——本地不再运行的
  记录删除、仍在运行的记录恢复 `running`、控制面已删除记录的本地运行时
  由 worker 侧清理。
  **2026-09-14 补正：**该扫描**不得触碰 `paused`**。暂停时 worker 用
  `killpg(SIGSTOP)` 冻结整棵进程组，而 SDK 唯一的解冻入口（`Sandbox.connect`
  的自动解冻）只在 `state == "paused"` 时投递 resume；一旦被抹成 `orphaned`，
  connect 连 resume 都不发 ⇒ **进程组永久停在 `T` 态，控制面却回 200 connected**，
  且日志对调用方完全静默（`recover_node` 只 `append_log`）。`paused` 在暂停时
  已归还配额、TTL 本就跳过，是唯一能推出解冻的状态，因此必须保态。
  修法见 `control_plane/registry/manager.py::mark_orphaned` 的
  `state != "paused"` 守卫；回归用例 `test_connect_auto_resume_survives_a_node_health_sweep`、
  `test_partition_leaves_a_paused_sandbox_paused`、`test_paused_sandbox_survives_a_stalled_worker_heartbeat`。
- **镜像 tag 非 digest**（低-中，供应链）：`E2B_BASE_IMAGE` 用 tag，
  tag 可被替换。已修复（E6.2）：`.env.example` 改 `@sha256:` 形式；
  `upgrade.sh` 校验 digest 格式（`--allow-tag-base-image` 显式放行 tag），
  tag 变更必须同步显式更新 digest；`build-and-push.sh` 推送后打印 digest。
- **MCP 端口只增不减**（低）：`_next_mcp_port` 从 51000 只增不回收，
  长期 worker 端口漂移。已修复（E6.3）：`McpPortPool` 空闲表 + 复用，
  沙箱删除/网关启动失败均归还端口，并发分配加锁不冲突。
- **命令 env 大小**：无限制（归 8.5）；已确认 `clean_env=True`，
  worker 环境变量（含 registry 密码）不泄漏进沙箱。
- **绝对 cwd**：无约束但 fs 策略兜底，无实际影响。

## 排期建议

1. **P0**：构建 + 部署已实现修复（内网隔离/redis/buildkit/限流）；同时加 TLS；
2. **P1**：租户隔离 + 独立 uid（配套 volume 权限模型）；
   PID namespace（`CLONE_NEWPID`）随独立 uid 一起评估（同属"沙箱内核级隔离"）；
3. **P2**：token 过期/失效、构建限流、internal key 轮换；
4. **P3**：命令输出缓存上限 + 文件写入 body 限制（内存 DoS）；
5. **P4**：worker 非 root、依赖锁定、元数据/产物限制、secret 加固；
6. **P5**：节点失联清理/对账、镜像 digest 固定、MCP 端口回收。
