# Sandlock + E2B 全部开发目标整体实施路线图

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Each task group below is executed as its own detailed plan (split out before execution); this document is the roadmap that fixes order, dependencies, files, and verification for every group.

**Goal:** 完成 sandlock fork 与本项目（E2B 服务端）全部 backlog 计划目标的开发与验证；按"先 sandlock、后本项目"两大阶段推进。"不推送远程"= 不做目标机远程部署；ACR 镜像推送照常，git 远程推送（上游 PR / origin push）暂缓，仅在最终交付时按需执行。

**Architecture:** 阶段一在 `third_party/sandlock`（Rust fork 子模块，运行时基线 `upstream-pr/netns-free-clean`）内完成内核级隔离（PID ns、独立 uid、per-sandbox 网络隔离）、回归与 wheel 矩阵；阶段二在本仓库 `control_plane/` `envd_service/` `gateway_common/` `deploy/` 完成部署安全修复、磁盘配额、租户隔离、资源管理、加固与收尾。两阶段通过"wheel 构建 → 子模块提交 → E2B 消费 wheel"衔接；网络隔离（S2 → E7）与独立 uid（S1.2 → E3.2）是跨阶段依赖链。

**Tech Stack:** Rust（sandlock-core/ffi/cli + PyO3 wheel）、CPython 3.14 运行时（worker/控制面）、FastAPI + Redis（控制面/共享状态）、XFS project quota（磁盘配额）、Docker/buildkit（构建与部署）、pytest（unit/contract/security/perf/sdk）。

## Global Constraints

- **"不推送远程"= 不做远程部署**（目标机 `upgrade.sh`、远程复测暂缓），**不是不推送镜像**：ACR 镜像推送照常执行；git 远程推送（主仓库 `origin` / sandlock fork `origin` / 上游 PR）仍暂缓，本地提交照常。
- sandlock 运行时基线固定为 `upstream-pr/netns-free-clean`（无 netns/veth 的无特权路径）；`feature/network-netns`（per-sandbox veth）仅作参考，不再作为运行时 wheel 基线。
- 全部 sandlock 目标必须在 **uid 65534 非 root** 形态下验证（入口 root 准备 + setpriv 降权）；新增 root 环境性失败为零。
- Java 无关；Python 代码统一 CPython 3.14（worker/控制面镜像 `python:3.14-slim`，wheel 按镜像内 ABI 选择）。
- 测试规范：断言必须精确（禁 toContain/includes 等部分匹配）、禁 SKIP/过滤错误输出、日志驱动排查、性能测试必录 profile（`tmp/perf/`）。
- 临时文件一律放项目内 `tmp/`（禁止系统 /tmp、$TMPDIR 写入）。
- 磁盘配额最终路线 = XFS project quota（已决策，不再评估 COW/du/LD_PRELOAD 路线）。
- per-sandbox 网络隔离最终路线 = 方案 1（loopback netns + supervisor fd 注入，PoC 已验证 ADDFD 无特权可行）。
- wheel 矩阵暂缓：只构建 **cp314 × x86_64/aarch64** 双架构（当前 `wheels/fork/` 基线）；cp310-313 扩列（M6 完整矩阵）不在本期计划内，后续按需再评估。
- E1 范围：**构建 + 推送 ACR 镜像 + 本地测试验证**（单测/契约/安全/本地 compose 冒烟）；目标机部署与远程复测暂缓（"不做远程部署"），待用户解除约束后单独执行。
- 每个任务组完成后：全量相关测试绿 + 子模块/主仓库本地提交（组粒度可再拆多次提交）。

---

## 执行顺序总览

```mermaid
graph LR
  S0[阶段一 S0 基线回归<br/>+ wheel 重建] --> S1[阶段一 S1 内核级隔离<br/>PID ns / 独立 uid / 无特权固化]
  S1 --> S2[阶段一 S2 per-sandbox 网络隔离<br/>loopback netns + fd 注入]
  S2 --> S3[阶段一 S3 wheel 交付<br/>cp314 双架构 / 私有源 / PR 整理]
  S3 --> E1[阶段二 E1 P0 安全修复<br/>推镜像 + 本地验证]
  E1 --> E2[阶段二 E2 XFS 磁盘配额]
  E2 --> E3[阶段二 E3 租户隔离与资源归属]
  E3 --> E4[阶段二 E4 内存 DoS]
  E4 --> E5[阶段二 E5 权限/供应链加固]
  E5 --> E6[阶段二 E6 运维一致性]
  E6 --> E7[阶段二 E7 网络隔离联动<br/>依赖 sandlock S2 产物]
  E7 --> E9[阶段二 E9 资源争用与驱逐]
  E9 --> E8[阶段二 E8 收尾回归]
```

跨阶段依赖链（必须先完成后一项才能开始后一项的**生产验证**）：

| 前置 | 后置 | 内容 |
|---|---|---|
| S1.2（独立 uid） | E3.2（每沙箱独立 uid + volume 权限模型） | uid 分配/回收与 worker 侧接入 |
| S1.3（无特权固化） | E5.1（worker/supervisor 非 root） | Dockerfile USER + 权限适配依赖 sandlock 无特权能力 |
| S2（per-sandbox 网络隔离） | E7（网络隔离联动） | MCP 路径/配置透传/集成验证 |
| O1（prjquota 启用） | E2 生产验证（M5） | 目标机挂载选项 |
| E1 部署 | E2-E9 的远程验证 | 生产先清 P0 漏洞，后续项才能在真实环境复测 |

---

# 阶段一：sandlock fork 全部目标（Phase S）

**代码库**：`third_party/sandlock`（子模块，当前 HEAD `a6d2060`，分支 `upstream-pr/netns-free-clean`）

**验证环境**：`sandlock-dev:latest` 容器（e2b-sandlock-test + rustup/rsproxy + iproute2）；入口 root 预置（`ip_unprivileged_port_start=0` + 198.18.0.99-103 回环 + /etc/hosts + chmod target）后 setpriv 降 uid 65534 跑测试；构建以 root 跑一次（`--user root --entrypoint bash`）。

## S0. 基线收尾（当前工作区状态固化）

### Task S0.1: Rust / 集成 / Python 全量回归基线

**Files:** 无（验证任务）
**验证命令:**
```bash
# 容器内，uid 65534（lib 基线 780 passed / integration 437 / python 430）
cargo test -p sandlock-core --lib --offline
cargo test --workspace --offline   # integration（netns 用例按能力跳过）
python -m pytest python/tests -q -p no:cacheprovider
```
**Expected:** 全绿，0 root 环境性失败（notify_rate_limit 改动 `a6d2060` 不破坏基线）。
**依赖:** 无
**提交:** 无（验证任务）

- [ ] 跑 lib / integration / python 三套，记录基线数字到 `docs/HANDOFF.md`

### Task S0.2: 重建双架构 wheel（含 notify_rate_limit）

**Files:**
- Build: `deploy/scripts/build-sandlock-wheels.sh`
- Output: `wheels/fork/sandlock-0.9.0b0-cp314-*-manylinux_2_34_*.whl`（x86_64 + aarch64）

**Interfaces:**
- Produces: wheel 含新符号 `sandlock_sandbox_builder_notify_rate_limit`；Python `Sandbox(notify_rate_limit=...)` 可原生构建
**验证:**
```bash
./deploy/scripts/build-sandlock-wheels.sh
python3 -c "import zipfile; z=zipfile.ZipFile('wheels/fork/sandlock-0.9.0b0-cp314-cp314-manylinux_2_34_x86_64.whl'); print([n for n in z.namelist() if 'libsandlock' in n])"
```
**Expected:** 双架构 wheel 生成，`auditwheel` 标签 manylinux_2_34 不变。
**依赖:** S0.1
**提交:** 无（wheel 不入库，见 .gitignore）

- [ ] 重建 wheel 并用 Python SDK 冒烟 `notify_rate_limit`

### Task S0.3: 子模块 + 主仓库指针本地提交

**Files:** `third_party/sandlock`（子模块指针）
**验证:** `git status --short` 干净；`git -C third_party/sandlock log -1` = `a6d2060`
**依赖:** S0.1（如回归发现新问题则修完再提交）
**提交:**
```bash
cd third_party/sandlock && git add -A && git commit -m "..." && cd ..
git add third_party/sandlock && git commit -m "chore: 子模块指针更新"
```

- [ ] 本地提交完成，不 push

## S1. 内核级隔离（backlog S1）

### Task S1.1: PID namespace（CLONE_NEWPID）

**Files:**
- Modify: `crates/sandlock-core/src/sandbox.rs`（spawn 路径）
- Modify: `crates/sandlock-core/src/sandbox/builder.rs`（flag/参数）
- Modify: `crates/sandlock-core/src/sys/structs.rs`（CLONE_NEWPID 已定义，接入 spawn）
- Modify: `crates/sandlock-core/src/seccomp/notif.rs`（pidfd / process_vm_readv / 进程组管理的 pid 引用）
- Modify: `crates/sandlock-core/src/procfs.rs`（PID 视图按新 ns 过滤）
- Test: `crates/sandlock-core/tests/integration/`（跨沙箱 PID 探测、信号隔离回归）

**Interfaces:**
- Produces: `SandboxBuilder.pid_ns(bool)`（或等价开关）；子进程实际 pid = host pid，沙箱内可见 pid 1 = 沙箱首进程
**验证:**
```bash
cargo test -p sandlock-core --lib --offline   # 新增 kill(pid,0) 探测用例
cargo test --workspace --offline
```
**Expected:** 沙箱内 `kill(pid,0)` 对宿主/其他沙箱进程返回 ESRCH（不再可枚举）；lib/integration 全绿；无 root 环境性失败。
**依赖:** S0.3
**提交:** 组内按"改一处验证一次"多次本地提交

- [ ] 设计两级 fork / clone(CLONE_NEWPID) 结构（对照 `docs/security-hardening.md` §1b）
- [ ] 接入 spawn 路径并适配 pid 引用（pidfd/process_vm_readv/进程组）
- [ ] procfs 视图按新 PID ns 过滤
- [ ] 新增跨沙箱 PID 探测测试（预期 ESRCH）+ 信号隔离回归
- [ ] 全量回归 + 子模块本地提交

### Task S1.2: 独立 uid（userns 单 entry）约束验证

**Files:**
- Modify: `crates/sandlock-core/src/credential.rs` / `RunAs` 路径（支持任意 uid，非固定 1000）
- Test: `crates/sandlock-core/tests/integration/`（不同 uid 沙箱文件互不可见、unix socket 隔离）

**Interfaces:**
- Produces: `RunAs::Uid(host_uid)` 在 userns 单 entry 映射下可用；沙箱内仍见 uid 0（映射后）
**验证:**
```bash
# 两个沙箱 RunAs 不同 host uid（如 10000/10001），同路径各写文件
cargo test --workspace --offline
```
**Expected:** 沙箱 A 读不到沙箱 B 的同名文件（0700 + 独立 uid）；unix socket 随 uid 隔离；regression 全绿。
**依赖:** S1.1（建议同组，共用 fork 结构改造）
**提交:** 本地提交

- [ ] 验证/修复 RunAs 任意 uid 的 userns 单 entry 映射
- [ ] 新增隔离用例（文件 + unix socket）+ 回归
- [ ] 输出 volume 共享权限模型的 sandlock 侧约束（供 E3.2 消费）

### Task S1.3: 无特权运行验证固化

**Files:**
- Modify: `deploy/docker/Dockerfile.test-runner` / 入口脚本（uid 65534 矩阵固化）
- Modify: `docs/HANDOFF.md`（基线数字）

**Interfaces:**
- Produces: 完整套件以 uid 65534 全绿的入口/测试矩阵说明
**验证:** 容器内全量（见 S0.1 命令），断言 0 root 环境性失败
**Expected:** lib / integration / python 三套在 uid 65534 下全绿。
**依赖:** S1.1、S1.2（新改动不破坏无特权路径）
**提交:** 本地提交（入口/文档改动）

- [ ] 全量无特权回归（含 S1.1/S1.2 新用例）
- [ ] 更新 HANDOFF 基线数字与"全程非 root"说明

## S2. per-sandbox 网络隔离（方案 1：loopback netns + fd 注入）

**方案文档:** `docs/netns-isolation-fd-injection.md`（PoC 已验证 ADDFD 无特权可行，注入 fd RTT 0.006ms / ~14Gbps）

### Task S2.1: connect handler ADDFD 注入改造

**Files:**
- Modify: `crates/sandlock-core/src/network/connect.rs`（`connect_on_behalf` → 宿主建连 + 注入）
- Modify: `crates/sandlock-core/src/seccomp/notif.rs`（`inject_fd_and_send` 已存在，接 connect 语义）
- Modify: `crates/sandlock-core/src/seccomp/dispatch.rs`（connect 返回 fd 号语义）
- Test: `crates/sandlock-core/tests/integration/test_netns.rs` 或新 `test_net_isolate.rs`

**Interfaces:**
- Produces: connect 通知被拦截后，supervisor 宿主建连 + `SECCOMP_ADDFD_FLAG_SEND` 注入，沙箱 `connect()` 返回 fd 号（非 0）——适配 PoC 发现的语义变化
**验证:**
```bash
cargo test -p sandlock-core --lib --offline
cargo test --workspace --offline
```
**Expected:** 沙箱内 connect 成功且数据面为注入 fd（echo 可达）；直连合成 IP 仍拒绝；回归全绿。
**依赖:** S1.3
**提交:** 本地提交

- [ ] connect handler 宿主建连 + ADDFD 注入（含返回 fd 语义）
- [ ] getsockname/getpeername 语义风险处理（见方案 §6.1，记录已知限制）
- [ ] 新增集成用例（注入 fd 数据面 echo）+ 全量回归

### Task S2.2: 沙箱创建 unshare(CLONE_NEWNET) + lo up

**Files:**
- Modify: `crates/sandlock-core/src/sandbox.rs`（spawn 路径加 CLONE_NEWNET，userns 之后）
- Modify: `crates/sandlock-core/src/sandbox/builder.rs`（`net_isolation(bool)` 开关）
- Modify: `crates/sandlock-core/src/network/`（共享 netns 路径保留为默认/兼容开关）

**Interfaces:**
- Produces: `SandboxBuilder.net_isolation(true)` 时沙箱进入独立 netns（仅 loopback）；默认 false 保持现有共享 netns 无特权路径
**验证:** 新集成用例：沙箱内仅 loopback；与宿主/其他沙箱 netns 不可见；`lo up` 由沙箱 userns 内完成（无需 CAP_NET_ADMIN）
**依赖:** S2.1
**提交:** 本地提交

- [ ] spawn 路径 CLONE_NEWNET + lo up（userns 内）
- [ ] 默认路径兼容回归（共享 netns 全量测试不回归）
- [ ] 新增 netns 隔离用例

### Task S2.3: DNS 适配（网关搬进沙箱 netns）

**Files:**
- Modify: `crates/sandlock-core/src/network/dns_synth.rs`（DNS 网关进程/任务归属）
- Modify: `crates/sandlock-core/src/network/connect.rs`（getaddrinfo 通知解析路径）

**Interfaces:**
- Produces: net_isolation 模式下 DNS 网关绑定沙箱 netns 内 `127.0.1.x:53`（沙箱 userns 内 bind，`ip_unprivileged_port_start` sysctl 不再需要）
**验证:** 沙箱内通配域名解析走合成 IP、连接走 S2.1 注入；网关在沙箱 netns 内可 bind；回归全绿
**依赖:** S2.2
**提交:** 本地提交

- [ ] DNS 网关归属沙箱 netns（或走 getaddrinfo 通知解析，二选一按实现评估）
- [ ] 通配域名 + 合成 IP + SSRF 护栏回归

### Task S2.4: UDP 适配

**Files:**
- Modify: `crates/sandlock-core/src/network/connect.rs` / `send` 路径（connected UDP 注入；datagram 沿用 on-behalf send）
- Test: 新增 UDP 集成用例（connected + datagram）

**验证:** connected UDP socket 可注入（数据面内核直连）；无 connect datagram 复用现有 on-behalf send 路径；QUIC 等场景验证
**依赖:** S2.3
**提交:** 本地提交

- [ ] connected UDP 注入 + datagram on-behalf 路径验证

### Task S2.5: 入站端口映射（必做，MCP 前置）

**Files:**
- Modify: `crates/sandlock-core/src/network/`（supervisor 宿主监听 50005+ → accept → 注入；**用户决策 2026-09-01：必做，作为 E7.1 MCP 路径前置**）
- Test: 新增集成用例（沙箱内 listen 的服务器经端口映射可被外部访问）

**验证:** 沙箱内 listen 的服务器经端口映射可被外部访问；MCP 场景验证；映射生命周期随沙箱创建/删除
**依赖:** S2.4
**提交:** 本地提交（如做）

- [ ] 实现端口映射（监听/accept/注入/清理）+ MCP 场景集成用例

### Task S2.6: 与现有网络特性集成回归

**Files:** 回归套件（通配域名 / HTTP MITM / maskRequestHost / egressProxy / 动态网络更新）
**验证:** 全量 lib / integration / python 三套全绿（uid 65534）；wheel 重建含 net_isolation
**依赖:** S2.1-S2.5
**提交:** wheel 重建 + 子模块本地提交

- [ ] 全特性矩阵回归 + wheel 重建

## S3. wheel 交付（cp314 双架构 + 上游 PR 准备）

### Task S3.1: cp314 × x86_64/aarch64 wheel 双架构验证

**Files:**
- Modify: `deploy/scripts/build-sandlock-wheels.sh`（沿用 zig 交叉编译流程，保持 cp314 双架构输出）
- Output: `wheels/fork/sandlock-0.9.0b0-cp314-*-manylinux_2_34_*.whl`（x86_64 + aarch64）

**验证:** cp314 双架构 wheel（含 S0.2 之后新增的 S1/S2 能力）在 `python:3.14-slim` 镜像 import + 建沙箱冒烟
**依赖:** S2.6
**提交:** 无（wheel 不入库）

- [ ] cp314 双架构重建 + 冒烟（cp310-313 扩列暂缓，不做）

### Task S3.2: 私有 index / git 安装切换（仅 cp314）

**Files:**
- Modify: `deploy/docker/Dockerfile.envd` / `Dockerfile.control-plane` / `Dockerfile.test-runner`（wheel 来源：私有 index 或 git+ssh 子模块引用）
- Modify: `requirements*.txt`

**验证:** `pip install` 从私有源安装 cp314 fork wheel；worker/测试镜像含最新能力
**依赖:** S3.1（仅 cp314 双架构）
**提交:** 本地提交

- [ ] 私有源/安装切换落地（cp314）+ 镜像构建验证

### Task S3.3: 上游 PR 分支整理与文案（推送暂缓）

**Files:** `docs/upstream-pr-netns-free.md`（更新 tip commit：含 S1/S2 可选，或保持 PR 范围不变仅记录）

**验证:** 分支 `upstream-pr/netns-free-clean` 与 PR 文案一致；`git status` 干净
**依赖:** S2.6
**提交:** 本地提交（不 push，PR 创建命令预留在文档中）

- [ ] 整理分支 + 更新 PR 文档 + 本地提交

---

# 阶段二：本项目（E2B 服务端）全部目标（Phase E）

**代码库**：本仓库（`control_plane/` `envd_service/` `gateway_common/` `deploy/` `tests/`）

**验证环境**：本地 pytest（unit/contract/sdk）+ Linux 容器全量（真实 Redis/registry/sandlock）+ 目标机远程复测

## E1. P0 部署已实现的安全修复（生产漏洞清零）

> 代码已提交（`917395b`，含 redis 认证 / 私网 deny / buildkit unix socket / create 限流）。
> **本期范围（按用户指示）**：完成构建 + **推送 ACR 镜像** + 本地测试验证；目标机部署与远程复测暂缓（"不推送远程"= 不做远程部署），待解除约束后执行。

### Task E1.1: 构建镜像并推送 ACR

**Files:**
- Run: `deploy/scripts/build-sandlock-wheels.sh`（已含 notify_rate_limit 的 wheel）
- Run: `deploy/scripts/build-and-push.sh`（构建 + 推送 ACR）

**验证:** 镜像 tag 更新到 `deploy/stack/.version`；worker/control-plane 镜像含新 wheel（`docker image inspect` 确认依赖版本）；`docker manifest inspect` 确认 ACR 上双架构
**依赖:** S0.2（wheel 含 notify_rate_limit）
**提交:** 无（产物不入库；版本文件随部署提交）

- [ ] 构建 + 推送 ACR + 镜像内容检查

### Task E1.2: 本地 compose 起栈冒烟（替代远程部署）

**Files:**
- Run: `docker compose -f deploy/stack/docker-compose.prod.yml up -d`（本地起栈，用本地构建镜像）
- Run: `deploy/scripts/deployment_smoke.py` / `multinode_smoke.py`（本地形态）

**验证:** 本地栈健康；redis 带 `requirepass`（`redis-cli -a` 可连、无密码被拒）；buildkit 无 TCP 监听（`unix://` 生效）；create 限流触发 429；私网 deny 生效
**依赖:** E1.1
**提交:** `.env`/`.version` 变更本地提交（不含敏感值）

- [ ] 本地起栈 + 四项修复冒烟（redis 认证 / buildkit sock / 限流 / deny CIDRs）

### Task E1.3: 本地安全回归（替代远程复测）

**Files:**
- Run: `tests/security/`（容器内全量，含网络隔离/模板隔离/进程限制）
- Run: `pytest tests/unit/test_network_config.py tests/unit/test_ratelimit.py`（新增单测）
- Run: `tmp/security-probe2.py` / `tmp/security-probe3.py`（如本地环境可执行，指向本地起栈）

**验证:** 单测 + 安全套件全绿；本地起栈下 OPEN 沙箱拒绝内网、限流生效（远程复测留待部署后）
**依赖:** E1.2
**提交:** 无

- [ ] 本地安全回归全绿，记录结果（远程复测标记为"待部署后"）

### Task E1.4: 控制面 API TLS

**Files:**
- Modify: `deploy/stack/docker-compose.prod.yml` / `deploy/compose/docker-compose.prod.yml`（代理层 TLS 或控制面 HTTPS）
- Modify: `control_plane/app.py`（如直接 HTTPS：`uvicorn ssl_certfile`）
- Test: `tests/contract/`（https 形态冒烟）

**验证:** 本地起栈下 `curl https://localhost:3000/health` 可用；HTTP 重定向或拒绝；测试套件含 https 冒烟
**依赖:** E1.2
**提交:** 本地提交

- [ ] TLS 方案落地（代理层或直连）+ 验证

## E2. P1 XFS project quota（方案已定稿）

**方案文档:** `docs/sandbox-disk-quota.md`（里程碑 M1-M5）、`docs/production-deployment-requirements.md`

### E2 前置决策：NFS 共享卷配额架构（决策记录 2026-09-01）

**结论**：共享卷若为 NFS 挂载，XFS project quota **仍可按目录生效**，前提是 **NFS 服务器端文件系统为 XFS 且启用 prjquota**（Rook NFS Provisioner 同款先例：服务器端建 project + xfs_quota 限额 + NFS 导出）。配额三个动作全在服务器端：

- **强制**：XFS 配额由服务器端内核执行，NFS 客户端（worker）写超限收到服务器返回的 EDQUOT（延迟写场景可能在 close() 返回，需沙箱内应用处理）；
- **继承**：目录设 `PROJINHERIT` + projid 后，新建 inode 继承父目录 project id 是 XFS 内核语义——NFS 客户端创建的文件走服务器端 create 路径，自动归入正确 projid，沙箱无感知；
- **管理**：`xfs_quota` 只能在 NFS 服务器本机执行，worker 对 NFS 挂载跑 xfs_quota 无效（RQUOTA/rquotad 仅覆盖 user/group，NFSv4 QUOTA_* 属性 Linux nfsd 未实现）。

由此派生四条实现约束（写进下面各 Task）：

1. **双配额域**：workspace（worker 本地 XFS）与共享卷（NFS 服务器 XFS）是两个独立配额域，`_xfs_project_supported` 必须分别检测——本地域查 worker 挂载点，NFS 域查**服务器端**文件系统（经 quota-agent）；
2. **quota-agent**：新增 NFS 服务器端配额管理通道（见 Task E2.6），worker/控制面经它执行 project 创建/删除/limit/孤儿清理；沙箱配额与 volume 配额共用；
3. **projid 全局唯一**：跨 worker 共享的 NFS 卷上，projid 由服务器端分配（计数器或 `hash(volume_id, sandbox_id)` 冲突规避），持久化到 `sandbox.json` / volume 记录；
4. **已知坑**：同一 ioctl 同时设 `FS_XFLAG_PROJINHERIT` 与 projid 时，先设 flag 会覆盖 projid——设置顺序必须先 projid 再 flag（或合并调用）。

NFS 服务器底层非 XFS（ext4/ZFS 等）时此路线不适用，降级到 ZFS dataset `userquota@uid`（per-dataset，需 E3.2 独立 uid）、CephFS 目录 quota 或软限（quota_mb 元数据 + 写前检查，仅准入不强制）。

### Task E2.1: `_xfs_project_supported` 检测 + 非 XFS 降级 + 单测

**Files:**
- Modify: `envd_service/`（新增 `xfs_quota.py` 或并入 config/manager）
- Test: `tests/unit/`（mock findmnt/xfs_info/xfs_quota）

**Interfaces:**
- Produces: `xfs_project_supported(mount_point, via_agent=False) -> tuple[bool, str]`；不满足 → 跳过 quota + 警告日志
- Consumes: `via_agent=True` 时经 quota-agent 检测 NFS 服务器端文件系统（服务器端 xfs / projid32bit / prjquota / 工具），worker 本地只看到 NFS 挂载
**验证:** mock 单测覆盖：本地域（非 xfs / projid32bit=0 / noquota / 缺工具）各返回 False+原因；NFS 域（经 agent 查询服务器端）同样覆盖；本地 XFS 生效、NFS 服务器端非 XFS → 降级跳过 + 警告日志
**依赖:** E1（部署前置）；O1（生产验证前置）
**提交:** 本地提交

- [ ] 双配额域检测函数 + 单测（本地直连 / agent 远程两形态）

### Task E2.2: 沙箱创建/删除 project 管理

**Files:**
- Modify: `envd_service/`（`agent_create_sandbox`/`agent_delete_sandbox` 调 `xfs_quota project/limit`；**共享存储为 NFS 形态时改经 quota-agent 在服务器端执行**）
- Modify: `control_plane/registry/` 与 `envd_service/` 的 `RuntimeSandbox`（`project_id: int | None` 持久化到 sandbox.json）

**验证:** OrbStack XFS loop 容器实测：创建沙箱有 project+limit；超限写报 EDQUOT；删除后 project 清理。NFS 形态：worker 本地 workspace 配额直连执行，共享卷配额经 agent 在服务器端执行
**依赖:** E2.1
**提交:** 本地提交

- [ ] projid 分配（哈希/计数器）+ 创建/删除命令 + 持久化
- [ ] OrbStack XFS 实测（`tmp/` 下建 xfs.img + losetup，参考方案 §4）
- [ ] NFS 形态走 quota-agent 的调用路径（本地直连与 agent 二选一按部署形态）

### Task E2.3: 命令串行锁

**Files:**
- Modify: `envd_service/process/manager.py`（per-sandbox `asyncio.Lock`）
- Test: `tests/unit/` + `tests/contract/`（并发命令排队/429）

**验证:** 同沙箱并发命令互斥；并发超过阈值返回 429
**依赖:** 独立（可与 E2.2 并行）
**提交:** 本地提交

- [ ] 串行锁实现 + 并发测试

### Task E2.4: 孤儿 project 清理 + 磁盘水位监控

**Files:**
- Modify: `envd_service/`（worker 启动扫描 sandbox.json 与 quota 表不一致的 project；**NFS 形态由 quota-agent 在服务器端对账清理**）
- Modify: `control_plane/`（磁盘水位监控/告警，可并入 metrics）

**验证:** 单测（mock quota 表）；worker 启动扫描清理孤儿
**依赖:** E2.2
**提交:** 本地提交

- [ ] 启动对账 + 定期巡检 + 告警
- [ ] NFS 形态服务器端对账（quota-agent 侧）用例

### Task E2.5: volume 独立配额

**Files:**
- Modify: `control_plane/`（volume 记录 `per_sandbox_quota_mb`：每沙箱在该卷的限额，创建时统一设定）
- Modify: `envd_service/`（volume 按沙箱子目录 `volume/<volume_id>/<sandbox_id>/` 挂载视图 + 每子目录独立 projid + limit；**NFS 形态经 quota-agent 在服务器端执行 project 管理**）
- Modify: `envd_service/executors/*`（挂载视图映射：沙箱看到自己的子目录；sandlock Landlock `fs_writable` 只放行该子目录作第二层兜底）
- Test: `tests/contract/`（多沙箱挂载同一卷：A 写爆限额不影响 B；超限 EDQUOT；删除沙箱清理子目录与 projid）

**验证:** volume 配额独立于沙箱配额；同卷多沙箱各自限额独立（A 超限 B 不受影响）；NFS 形态下服务器端强制 + EDQUOT 传播
**依赖:** E2.4
**提交:** 本地提交

- [ ] `per_sandbox_quota_mb` 元数据 + 每沙箱子目录 project 管理（projid 全局唯一）
- [ ] 挂载视图改造 + Landlock 兜底
- [ ] 多沙箱同卷独立限额测试（本地 XFS + NFS 服务器端两形态）

> 用户决策 2026-09-01：`per_sandbox_quota_mb` 在 **volume 创建时统一设定**，所有挂载沙箱同额；不做挂载时单独指定。

### Task E2.6: quota-agent（NFS 服务器端配额管理通道）

**Files:**
- Add: `deploy/`（NFS 服务器端配额 agent：监听 worker/控制面的配额管理请求，本地执行 `xfs_quota`；SSH 通道或小 HTTP 服务二选一，按部署形态定）
- Modify: `envd_service/` / `control_plane/`（配额操作客户端：本地 XFS 直连，NFS 形态转发 agent）
- Test: `tests/unit/`（mock agent 协议：project 创建/删除/limit/对账/检测）

**Interfaces:**
- Produces: agent 请求面：`detect(mount)` / `project_create(projid, path, limit_mb)` / `project_delete(projid, path)` / `reconcile()`；worker 侧配额操作统一封装（直连或 agent 由部署配置切换）
**验证:** 单测覆盖协议与错误路径（agent 不可达 → 降级跳过 + 告警）；OrbStack NFS 服务器容器实测 project 创建/限额/EDQUOT 传播
**依赖:** E2.1（检测经 agent）；E2.5（volume 配额经 agent）
**提交:** 本地提交

- [ ] agent 实现 + 协议单测
- [ ] worker 侧封装（直连/agent 切换）+ NFS 服务器容器实测

## E3. P2 租户隔离与资源归属

### Task E3.1: 租户隔离（授权层）

**Files:**
- Modify: `control_plane/auth.py`（`tenant_of` / `E2B_TENANTS` / `E2B_ADMIN_API_KEYS` 解析）
- Modify: `control_plane/registry/*`（各记录加 `tenant_id` + 持久化）
- Modify: `control_plane/api/*`（列表过滤 + 单资源归属校验 helper `_require_owned` + 跨资源 403）
- Modify: `control_plane/registry/sandboxes.py`（tenant 维度配额记账 + 准入）
- Modify: `control_plane/ratelimit.py` / `control_plane/api/sandboxes.py`（tenant 维度限流）
- Add: `control_plane/api/internal_tenants.py`（`GET /internal/tenants` 用量/配额）
- Add: `deploy/scripts/migrate-tenants.py`（存量资源归属迁移）
- Test: `tests/unit/` + `tests/contract/`（按方案 §7 矩阵）

**Interfaces:**
- Consumes: `tenant_of(request) -> tuple[str|None, bool]`（tenant, is_admin）
- Produces: `_require_owned(request, record)`；资源记录 `tenant_id` 字段；`E2B_TENANTS`/`E2B_ADMIN_API_KEYS`/`E2B_TENANT_LIMITS`/`E2B_TENANT_RATE_LIMITS` 配置

**验证:** 方案 §7 测试矩阵全绿（列表过滤 / 404 防泄露 / 跨资源 403 / admin 全量 / 兼容模式 / 配额隔离 / tenant 限流 / 迁移脚本）
**依赖:** E2（资源记账基线稳定后）
**提交:** 按 registry → auth → API → 配额 → 限流 → 管理端点 → 迁移脚本 分多次本地提交

- [ ] registry 字段 + 持久化
- [ ] auth 解析 + API 授权
- [ ] tenant 配额 + 限流
- [ ] 管理端点 + 迁移脚本 + 全量测试

### Task E3.2: 每沙箱独立 uid + volume 权限模型

**Files:**
- Modify: `envd_service/`（uid 分配/回收池、`sandbox.json` 持久化、rootfs 属主处理）
- Modify: `control_plane/`（volume 属主策略：0777+sticky 或创建者 uid）
- Test: `tests/security/` + `tests/contract/`

**验证:** 不同沙箱同路径文件互不可见；volume 跨沙箱共享按权限模型可用；孤儿 uid 清理
**依赖:** S1.2（sandlock 侧）；E3.1（资源归属语义）
**提交:** 本地提交

- [ ] worker uid 池 + 沙箱 RunAs
- [ ] volume 权限模型 + rootfs 属主适配
- [ ] 安全/契约测试 + 回归

### Task E3.3: volume token 过期/吊销

**Files:**
- Modify: `control_plane/registry/volumes.py`（token 加 TTL/吊销状态 + Redis 持久化）
- Test: `tests/unit/` + `tests/contract/`

**验证:** 过期/吊销后访问 401/404；现有 token 语义兼容
**依赖:** E3.1
**提交:** 本地提交

- [ ] token TTL/吊销 + 测试

### Task E3.4: template upload token 上传后失效

**Files:**
- Modify: `control_plane/registry/templates.py`（`mark_file_uploaded` 清除 token + 幂等）
- Test: `tests/contract/`（重复 PUT 覆盖被拒）

**验证:** 上传成功后 token 失效；重复上传返回错误
**依赖:** E3.1
**提交:** 本地提交

- [ ] token 失效 + 测试

### Task E3.5: 模板构建并发/资源限流

**Files:**
- Modify: `control_plane/api/templates.py`（并发上限 + 构建队列/限流）
- Test: `tests/contract/`

**验证:** 并发构建超限返回 429/503；构建资源受限
**依赖:** E3.1
**提交:** 本地提交

- [ ] 构建限流 + 测试

### Task E3.6: internal key 轮换机制

**Files:**
- Modify: `control_plane/auth.py`（多 key 支持 + 轮换窗口）
- Modify: `deploy/scripts/upgrade.sh`（轮换流程）
- Test: `tests/contract/`

**验证:** 新旧 key 轮换窗口内均可用；轮换后旧 key 失效
**依赖:** E3.1
**提交:** 本地提交

- [ ] 多 key + 轮换流程 + 测试

## E4. P3 内存 DoS 修复

### Task E4.1: 命令输出缓存上限

**Files:**
- Modify: `envd_service/process/manager.py`（`captured` capped buffer，如 10MB 截断）
- Test: `tests/unit/` + `tests/contract/`

**验证:** `cat /dev/zero` 场景缓存封顶；截断行为精确断言
**依赖:** 无
**提交:** 本地提交

- [ ] capped buffer + 测试

### Task E4.2: 文件写入 body 限制 + 流式写盘

**Files:**
- Modify: `control_plane/api/volumes.py` / `templates.py`（`await request.body()` → 流式 + `Content-Length` 限制）
- Modify: `envd_service/`（worker `files.write` 同款改造）
- Test: `tests/contract/`（超限 413）

**验证:** 超大上传被 413 拒绝；正常上传流式写盘；worker 内存不随 body 增长
**依赖:** 无
**提交:** 本地提交

- [ ] 流式写盘 + 大小限制 + 测试

## E5. P4 权限/供应链/加固

### Task E5.1: worker/supervisor 非 root

**Files:**
- Modify: `deploy/docker/Dockerfile.envd`（`USER` + 权限适配）
- Modify: `deploy/compose/*.yml` / `deploy/stack/*.yml`（securityContext/user）
- Test: `tests/security/`（容器内 uid 断言）

**验证:** worker 容器内 supervisor 非 root（如 uid 65534）；沙箱创建/网络/rootfs 全绿
**依赖:** S1.3（sandlock 无特权能力）；E1 部署基线
**提交:** 本地提交

- [ ] Dockerfile USER + 权限适配 + 回归

### Task E5.2: 核心依赖锁版本

**Files:**
- Modify: `requirements.txt` / `requirements-test.txt`（`==` 锁定或 lockfile）

**验证:** `pip install -r` 可复现；测试全绿
**依赖:** 无
**提交:** 本地提交

- [ ] 锁定版本 + 复现验证

### Task E5.3: metadata/envVars/产物大小限制

**Files:**
- Modify: `control_plane/api/sandboxes.py` / `templates.py` / `snapshots.py`（大小校验）
- Test: `tests/contract/`

**验证:** 超限 413/400；sandbox.json 不膨胀
**依赖:** E4.2（流式写盘）
**提交:** 本地提交

- [ ] 大小限制 + 测试

### Task E5.4: secret 加密/持久化 + 凭据管理

**Files:**
- Modify: `control_plane/registry/secrets.py`（加密持久化到 Redis + master key 配置）
- Modify: `deploy/scripts/upgrade.sh` / 部署文档（凭据上密钥管理）
- Test: `tests/unit/` + `tests/contract/`

**验证:** 重启后 secret 不丢；落盘值加密；key 轮换可用
**依赖:** E3.1（tenant_id 语义）
**提交:** 本地提交

- [ ] secret 持久化/加密 + 测试

## E6. P5 运维一致性

### Task E6.1: 节点失联沙箱清理/worker 恢复对账

**Files:**
- Modify: `control_plane/registry/nodes.py`（unhealthy 触发清理）
- Modify: `envd_service/`（worker 恢复对账：沙箱记录 vs 本地运行时）
- Test: `tests/contract/`（多节点分区场景）

**验证:** 失联节点沙箱被清理或标记；worker 恢复后记录一致
**依赖:** E1（部署形态）
**提交:** 本地提交

- [ ] 失联清理 + 对账 + 测试

### Task E6.2: 镜像 digest 固定

**Files:**
- Modify: `deploy/stack/.env.example` / `deploy/compose/.env.example`（`E2B_BASE_IMAGE=...@sha256:`）
- Modify: `deploy/scripts/upgrade.sh`（digest 解析/校验）

**验证:** 部署使用 digest；tag 变更需显式更新
**依赖:** 无
**提交:** 本地提交

- [ ] digest 配置 + 校验

### Task E6.3: MCP 端口回收复用

**Files:**
- Modify: `envd_service/`（`_next_mcp_port` 分配 → 空闲表 + 复用）
- Test: `tests/unit/` + `tests/contract/`

**验证:** 沙箱删除后端口可复用；并发分配不冲突
**依赖:** 无
**提交:** 本地提交

- [ ] 端口表 + 复用 + 测试

### Task E6.4: 真实 NFS/CSI 部署验证（共享存储形态）

**Files:**
- Run: `deploy/scripts/multinode_smoke.py` / `deployment_smoke.py`（NFS 挂载形态）
- Modify: `docs/production-deployment-requirements.md`（NFS 部署注意事项：root_squash / uid 映射 / 命令 IO 延迟）

**验证:** 多节点 NFS 下：共享 workspace/volume 路径语义一致、迁移保留文件、命令 IO 正常；**配额专项（NFS 服务器端 XFS + prjquota）**：
- NFS 客户端（worker）在 `volume/<sandbox_id>/` 创建的文件 projid 正确继承（`xfs_quota report -p` 或服务器端 `lsattr -p` 核对）；
- 沙箱写超限 → 服务器端返回 EDQUOT 并经 NFS 传播（延迟写 close() 场景一并验证）；
- 多 worker 同时写同一卷 → 各自子目录限额独立、projid 不冲突；
- root_squash / uid 映射对 projid 继承与配额记账的影响实测，结论写入文档

**依赖:** E2.6（quota-agent）；E2.5（volume 配额）；E6.1（对账）
**提交:** 本地提交（文档/脚本）

- [ ] NFS 环境实测（路径语义/迁移/IO + 配额专项四用例）+ 结论记录

## E7. 网络隔离联动（依赖 sandlock S2）

### Task E7.1: MCP/gateway 到沙箱内 MCP 的路径适配

**Files:**
- Modify: `gateway_common/` / `envd_service/`（沙箱 netns 内 MCP 端口可达性）
- Test: `tests/contract/`（MCP 连接）

**验证:** SDK → gateway → 沙箱内 MCP 全链路可用（含入站映射，如 S2.5 实现）
**依赖:** S2.5/S2.6（wheel 含 net_isolation）
**提交:** 本地提交

- [ ] MCP 路径适配 + 测试

### Task E7.2: 网络配置透传与全量回归

**Files:**
- Modify: `envd_service/executors/sandlock.py` / `gateway_common/network.py`（`net_isolation` 配置透传）
- Test: `tests/security/` + `tests/contract/` + `tests/sdk/`（全量）

**验证:** 全量套件在 net_isolation 开启下全绿；默认关闭兼容
**依赖:** E7.1
**提交:** 本地提交

- [ ] 透传 + 全量回归

### Task E7.3: Linux 集成验证

**Files:** 容器全量（真实 Redis/registry/sandlock）+ 目标机复测
**验证:** 全量测试 + 远程 smoke 在网络隔离形态下通过
**依赖:** E7.2
**提交:** 无

- [ ] Linux 容器全量 + 远程验证

## E9. 资源争用与驱逐

> 用户决策 2026-09-01：驱逐机制**默认开启**（`E2B_EVICTION_ENABLED` 默认 `true`），仅空闲阈值与优先级可配置；方案文档 `docs/resource-contention.md` §8 的"默认关闭"建议不采纳。

### Task E9.1: 空闲检测

**Files:**
- Modify: `control_plane/registry/sandboxes.py`（`last_active_at` 字段 + 命令/API 更新）
- Modify: `envd_service/`（活跃上报）
- Test: `tests/unit/` + `tests/contract/`

**验证:** 活跃时间正确更新；空闲判定阈值配置生效（`E2B_SANDBOX_IDLE_THRESHOLD_S` 默认 300）
**依赖:** E3.1（tenant 维度可选的驱逐权重）
**提交:** 本地提交

- [x] last_active_at + 上报 + 测试

### Task E9.2: pause 释放配额改造（前置）

**Files:**
- Modify: `control_plane/api/sandboxes.py`（pause 释放配额、resume 重新分配）
- Modify: `envd_service/`（pause 持久化现场）
- Test: `tests/contract/`

**验证:** pause 后配额释放；resume 配额不足时失败/排队
**依赖:** E9.1
**提交:** 本地提交

- [x] pause/resume 配额语义 + 测试

### Task E9.3: 驱逐选择器 + kill/pause + 通知

**Files:**
- Modify: `control_plane/registry/nodes.py` / `control_plane/api/sandboxes.py`（选择器：优先级 + 空闲最久 + 租户权重）
- Modify: `envd_service/`（驱逐动作 + 通知事件）
- Test: `tests/contract/`

**验证:** `E2B_EVICTION_ENABLED` 默认 true：资源满 + 空闲低优先级 → 驱逐成功（带通知）；全部活跃 → 503/排队；显式置 false 时不做驱逐
**依赖:** E9.2
**提交:** 本地提交

- [x] 选择器 + 动作 + 通知 + 测试

### Task E9.4: 创建排队/分层池

**Files:**
- Modify: `control_plane/api/sandboxes.py`（排队 + 超时 `E2B_CREATE_QUEUE_TIMEOUT_S`）
- Test: `tests/contract/`

**验证:** 资源释放后自动补建；超时失败；与幂等创建兼容
**依赖:** E9.3
**提交:** 本地提交

- [x] 排队 + 超时 + 测试

## E8. 收尾

### Task E8.1: 部署后远程 smoke 回归

**Files:** `deploy/scripts/deployment_smoke.py` / `multinode_smoke.py`
**验证:** 迁移/共享 workspace/network 更新/驱逐/配额 全部远程通过
**依赖:** E9、E7、E2（部署形态）
**提交:** 无

- [ ] 远程 smoke 全绿

### Task E8.2: 本地测试基线确认

**Files:** `docs/HANDOFF.md`（基线数字更新）
**验证:** unit + contract + sdk + security + perf 全量基线（Linux 容器）
**依赖:** 全部
**提交:** 本地提交（文档）

- [x] 基线确认 + HANDOFF 更新

---

# 运维侧任务（配合各阶段）

| # | 任务 | 触发时机 | 状态 |
|---|---|---|---|
| O1 | 目标机启用 XFS `prjquota`（fstab + 在线 remount，见 production-deployment-requirements.md） | E2 生产验证（M5）前 | 未开始（需维护窗口） |
| O2 | TLS 证书/代理层配置 | E1.4 | 未开始 |
| O3 | 凭据管理（ACR 密码/API key/redis 密码/SSH 口令上密钥管理） | E5.4 | 未开始 |

---

# 完成定义（Definition of Done）

- 阶段一：S0-S3 全部任务完成；sandlock 子模块所有改动本地提交；wheel 矩阵构建通过；上游 PR 分支整理完毕（推送按用户指示暂缓）。
- 阶段二：E1-E9 全部任务完成；每任务组相关测试全绿；部署形态远程 smoke 通过；HANDOFF/backlog 状态全部更新为已完成；主仓库本地提交完成，无未提交改动。
- 全程：不做目标机远程部署（`upgrade.sh` / 远程复测），除非用户解除约束；ACR 镜像推送照常；git 远程推送（origin / 上游 PR）暂缓。

# 风险与假设

1. **S2（per-sandbox 网络隔离）是 sandlock 核心路径改造（2-4 周量级）**：connect 语义变化（fd 注入返回 fd 号）影响面大，S2.1 先以独立开关 + 默认兼容推进，避免阻塞其余任务。
2. **S1.1（PID ns）与 S1.2（独立 uid）共用 fork 结构改造**：建议同组实施；若回归面过大，独立 uid（S1.2）可先于 PID ns（S1.1）交付（E3.2 只依赖 S1.2）。
3. **E1 已按用户指示调整**："不推送远程" = 不做远程部署（目标机 `upgrade.sh` / 远程复测暂缓）；ACR 镜像推送照常执行；远程部署留待用户解除约束后单独执行（届时仍需运维窗口）。
4. **租户隔离（E3.1）约 1.5-2 周**：控制面改动最大项，按 §7 矩阵分步提交。
5. **O1（prjquota 在线启用）可回退**（`remount,noquota`），无持久副作用；但需维护窗口，且与内核升级联动评估。
6. **wheel 矩阵已按用户指示缩为仅 cp314 双架构**（M6 完整矩阵暂缓）：后续如需 cp310-313，沿用 zig + auditwheel 流程、以 cp314 已验证配置为模板再扩；本期不阻塞任何任务。
7. **NFS 共享卷配额依赖服务器端 XFS**：NFS 服务器底层非 XFS 时 project quota 路线不适用，需降级 ZFS/CephFS/软限（见 E2 前置决策）；quota-agent 是 NFS 形态的强制组件，其可用性直接决定 NFS 下配额是否生效。
