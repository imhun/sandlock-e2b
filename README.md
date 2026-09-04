# E2B-Sandlock 网关

本地 E2B 兼容层：官方 `e2b` Python/JS SDK 零代码修改，控制面复刻官方
Sandbox REST API，envd 复刻官方 Connect-RPC 与文件 HTTP 契约，底层用
Sandlock（Landlock + seccomp-bpf + seccomp user notification）执行用户代码。

详细方案见 [spec.md](spec.md)。

## 架构

```text
SDK (Python 2.46.x / JS 2.46.1)
  |
  | E2B_API_URL
  v
Control Plane :3000          -- 沙箱注册表 / TTL / 认证 / 资源准入
  |
  | E2B_SANDBOX_URL
  v
Envd Service :49983          -- Connect-RPC + /files /health /envs /metrics
  |
  v
Executor                     -- Sandlock（Linux 6.12+）/ Local（开发回退）
```

## 快速开始（macOS 开发，Local 执行器）

```bash
python3 -m venv tmp/venv
tmp/venv/bin/pip install -r requirements-test.txt

# 终端 1：控制面
E2B_API_KEYS=local-key tmp/venv/bin/python -m control_plane

# 终端 2：envd
E2B_EXECUTOR=local tmp/venv/bin/python -m envd_service
```

官方 SDK 只设置三个环境变量即可接入：

```python
import os
os.environ["E2B_API_URL"] = "http://localhost:3000"
os.environ["E2B_SANDBOX_URL"] = "http://localhost:49983"
os.environ["E2B_API_KEY"] = "local-key"

from e2b import Sandbox

sandbox = Sandbox.create()
result = sandbox.commands.run("python3 -c 'print(1+1)'")
assert result.stdout == "2\n"
sandbox.kill()
```

## 测试

| 层 | 命令 | 环境 |
|----|------|------|
| L1 单元 + L2 契约 | `pytest tests/unit tests/contract` | macOS / Ubuntu |
| L3 Python SDK | `pytest tests/sdk/python` | Linux test runner（Sandlock）；macOS 可用 Local 执行器跑通协议 |
| L3 JS SDK | `pytest tests/sdk/js`（内部 `npm test`） | macOS / Linux；首次需 `cd tests/sdk/js && npm install` |
| L4 安全 | `E2B_BASE_IMAGE=python:3.14-slim pytest tests/security` | 容器运行时 VM 需 Landlock ABI ≥6（内核 6.12+）+ Docker daemon |
| L5 性能 | `E2B_BASE_IMAGE=python:3.14-slim pytest tests/perf --perf` | Linux + Docker；profile 写入 `tmp/perf/` |

本地直连远程部署实例跑 SDK 测试（无需起本地服务）：见
[docs/remote-testing.md](docs/remote-testing.md)。

Linux test runner：

```bash
docker compose -f deploy/compose/docker-compose.test.yml build
docker compose -f deploy/compose/docker-compose.test.yml run --rm test-runner pytest tests/sdk/python
```

全量验收直接跑容器，**非特权形态**：不挂 `--privileged`，改用
`seccomp=unconfined`（sandlock 需要用户命名空间，Docker 默认 seccomp 会
EPERM）+ `--cap-add NET_ADMIN`（仅通配域名本地 origin fixture 需要在 lo
挂 198.18.0.99）。`--network host` 仅用于测试基础设施（registry/Redis 容器
发布在宿主 localhost，Docker daemon 也只对 localhost 默认放行 HTTP
registry），不是 sandlock 的需要：

```bash
docker run --rm --security-opt seccomp=unconfined --cap-add NET_ADMIN --network host \
  -e E2B_BASE_IMAGE=python:3.14-slim \
  -e E2B_HOST_PROJECT="$(pwd)" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$(pwd):/workspace" -w /workspace \
  e2b-sandlock-test:latest pytest tests --perf -p no:cacheprovider
```

`E2B_HOST_PROJECT` 让容器内执行的 docker CLI 能拿到宿主侧的项目路径
（用于给带认证的 registry 挂载 htpasswd 文件）。

验收环境只看**容器运行时 VM 的内核**，不看宿主是 macOS 还是 Linux：

```bash
docker run --rm --security-opt seccomp=unconfined e2b-sandlock-test:latest \
  python3 -c "import sandlock; print(sandlock.landlock_abi_version())"   # 必须 >= 6
```

- **>= 6 即可以此为 Sandlock 验收环境**（ABI v6 对应内核 6.12+）。本机实测：
  OrbStack `7.0.14-orbstack` 容器内 **ABI = 8** ⇒ 隔离 / 网络 / exec / 记账面都能在本机容器验收；
  历史上 E8.2/E8.3 的"Linux 容器全量"基线本来也都是通过 OrbStack 的 docker.sock 跑的
  （见 `docs/HANDOFF.md` 的验证命令）。
- **旧版 Docker Desktop 的 Linux VM 常低于该 ABI** ⇒ 那种机器上不得把本机当作 Sandlock 验收环境：
  宿主 macOS 本身永远跑不了 sandlock（Landlock/seccomp 仅 Linux），此时 macOS 宿主只跑
  不依赖 sandlock 的单元/契约测试，SDK 测试用 Local 执行器验证协议兼容性，隔离能力换真 Linux 验收。
- **与运行时无关、本机一律不可用的**：XFS project quota 类用例（容器根是 overlay、无
  `/dev/loop-control`，启动会打印 `XFS gates unavailable`）⇒ 属真机/运维项（`docs/sandbox-disk-quota.md`、
  `docs/production-deployment-requirements.md`）。

Dockerfile 使用清华 apt/pip 镜像源、JS SDK 测试使用 npmmirror 源，构建与安装
走国内网络。test runner 容器需要 `privileged: true`（Docker 默认 seccomp
profile 会拦截 sandlock 安装自己的 seccomp 过滤器，`deploy/compose/docker-compose.test.yml`
已配置）。

## 容器镜像与部署

**镜像**：
- `deploy/docker/Dockerfile.control-plane-gateway` 打包**控制面 + envd gateway 合并镜像**
  （`gateway_common` + `control_plane` + `envd_service`，单服务单端口
  `:3000` 同时服务 API 与沙箱路由，`E2B_API_URL` 与 `E2B_SANDBOX_URL`
  指向同一地址，不带 sandlock wheel）；
- `deploy/docker/Dockerfile.envd` 只打包 worker（`gateway_common` + `envd_service`，含
  **fork sandlock wheel**（`wheels/fork/`，按 TARGETARCH 选择；通配
  allowOut / header 注入 / host 掩码 / SOCKS5 on-behalf）与 mcp-gateway）。

控制面与 worker 在代码层已解耦（env 工具函数收敛到 `gateway_common.env`；
控制面在分离模式下用 no-op runtime registry），所以合并镜像不依赖
sandlock wheel、worker 镜像不依赖 control_plane。`deploy/docker/Dockerfile.control-plane`
仅保留给单机本地构建示例（`deploy/compose/docker-compose.yml`）使用。

`wheels/fork/` 是构建产物、不入库（fork 源码固定于 `third_party/sandlock`
子模块）：构建镜像前先执行 `./deploy/scripts/build-sandlock-wheels.sh` 生成 wheel。

**构建（多架构）**：

```bash
# 单平台加载到本地 docker
REGISTRY=e2b-sandlock VERSION=1.0 PLATFORMS=linux/amd64 ./deploy/scripts/build-images.sh

# 多架构（x86_64 + arm64）需推送到 registry
REGISTRY=registry.example.com/e2b \
VERSION=1.0 \
PLATFORMS=linux/amd64,linux/arm64 \
PUSH=1 ./deploy/scripts/build-images.sh
```

产出镜像，**名称区分服务、tag 区分版本**：
`<registry>/e2b-sandlock-control-plane-gateway:<version>`（合并服务，见
`deploy/scripts/build-and-push.sh`）、`<registry>/e2b-sandlock-worker:<version>`、
`<registry>/e2b-sandlock-autoscaler:<version>`。

**生产部署示例**（合并控制面/gateway + 多 worker + Redis 共享状态 + 可选本地
镜像仓库）：

```bash
cp deploy/compose/.env.example deploy/compose/.env   # 修改密钥/端口/仓库
docker compose -f deploy/compose/docker-compose.prod.yml up -d --build

# 冒烟验证（沙箱跨节点分布、经 gateway 的命令/文件/stdin、kill 后配额释放）
E2B_API_URL=http://127.0.0.1:3000 \
E2B_SANDBOX_URL=http://127.0.0.1:3000 \
E2B_API_KEY=local-key \
python deploy/scripts/multinode_smoke.py

# 部署级验证（追加：跨 worker 迁移 + 共享 workspace 文件保留 + network
# 配置回显/原子更新，三 worker 分布）
E2B_API_URL=http://127.0.0.1:3000 \
E2B_SANDBOX_URL=http://127.0.0.1:3000 \
E2B_API_KEY=local-key \
python deploy/scripts/deployment_smoke.py
```

生产形态容器冒烟（非 privileged + `seccomp=unconfined`，验证沙箱创建 /
rootfs chroot / SOCKS5 出口，与 compose 部署一致）：

```bash
./deploy/scripts/smoke-prod-worker.sh
```

要点：
- worker 需要 `security_opt: [seccomp=unconfined]`（嵌套 seccomp 过滤器）与
  Docker daemon socket（模板 rootfs 解析）；`E2B_ENABLE_NETWORK=true` 时
  网络 API 策略才生效；
- 控制面设置 `E2B_SHARED_WORKSPACE_ROOT` + worker 挂同一存储（示例用
  named volume 模拟；跨机部署指向同一 NFS/CSI 挂载），迁移只切路由；
- `E2B_REDIS_URL` 指向 Redis 后多控制面副本共享注册表/配额/迁移锁；
- 模板镜像仓库（`E2B_IMAGE_REGISTRY`）留空时保持单机行为；启用本地
  `registry` 服务（`--profile registry`）需把其地址加入 daemon
  insecure-registries；
- macOS 冒烟如遇宿主端口占用，通过 `.env` 的 `CONTROL_PLANE_PORT` /
  `GATEWAY_PORT` 换端口；Docker Desktop/OrbStack 用户把 `DOCKER_SOCK`
  指向本机 docker.sock。

## 环境变量

控制面与 envd 的全部配置见 spec §7.2，默认值与之一致。常用：

| 变量 | 默认 | 说明 |
|------|------|------|
| `E2B_API_KEY` / `E2B_API_KEYS` | `local-key` | 控制面 API Key |
| `E2B_WORKSPACE_BASE` | `tmp/sandboxes` | 沙箱工作目录 |
| `E2B_BASE_IMAGE` | 未配置 | `base` 模板基础镜像；配置后启用镜像 rootfs |
| `E2B_TEMPLATE_IMAGES` | `{}` | 模板 ID → 基础镜像 JSON 映射 |
| `E2B_EXECUTOR` | `auto` | `auto`/`local`/`sandlock` |
| `E2B_MAX_TOTAL_*` | 见 spec | 宿主总资源上限，`0` 表示关闭该维度 |

## 与 spec 的两处事实性偏差

1. **`envdVersion` 使用 `0.6.4+sandlock`**：spec 原文为 `0.6.4-sandlock`，
   但官方 SDK 用 `packaging.Version` 解析该字段，`0.6.4-sandlock` 不是合法
   PEP 440 版本号，会在 `Sandbox.create()` 时抛 `InvalidVersion`。
   `0.6.4+sandlock` 是合法的 PEP 440 local version，语义不变且 SDK 可解析。
2. **PyPI 上 `e2b` 最高版本为 `2.46.0`**（`2.46.1` 不存在），测试按
   `e2b==2.46.0` 安装；JS 侧 `e2b@2.46.1` 存在，按 spec 使用。

## 实现说明

- 双服务独立启动：`python -m control_plane` 与 `python -m envd_service`。
- Sandlock 一个实例同一时刻只跑一个命令，Envd 进程管理器为每条命令创建独立
  Sandlock 实例；沙箱目录通过 `fs_writable` 共享（非 COW），跨命令持久化。
- 模板配置基础镜像时，envd 通过 Docker daemon 导出镜像 rootfs 并用 Sandlock
  `chroot` + `fs_mount` 执行；未配置镜像时纯 Sandlock（无 Root/容器依赖）。
- slim 基础镜像不含 `bash`，而官方 SDK 固定发送 `cmd=/bin/bash`；Sandlock
  执行器在 rootfs 内无 bash 时自动回退 `/bin/sh`，保证命令语义。
- PTY 已通过 spike 验收：Local 执行器用宿主 pty；Sandlock 执行器通过「沙箱内
  pty 桥」实现（沙箱内创建真实 pty，命令挂到 slave，master 数据经 PIPED stdio
  转发，resize 走带内控制帧）。限制：桥需要沙箱内有 Python 3 解释器
  （python 系镜像与纯 Sandlock 环境满足；node 系镜像暂不支持）。
- 不支持的 API（fork/snapshots/templates 等）一律返回官方 Error JSON
  （`501`），不返回假成功。

## v2.1 扩展功能说明与限制

- **Volume**：`Volume.create/connect/list/get_info/destroy` + volumecontent
  文件 API（`E2B_VOLUME_API_URL`，`Authorization: Bearer <token>`）+ 沙箱
  挂载。**路径规则在所有执行器/模板下统一**：挂载路径按沙箱根规范化（前导
  `/` 自动去掉，`/mnt/data` 与 `mnt/data` 等价），挂载点位于沙箱根内；命令
  默认 cwd 即沙箱根，用相对路径访问（如 `cat mnt/data/x.txt`）。纯 Sandlock
  用 symlink 实现（无 root 依赖），镜像 rootfs 模式映射到 chroot 内沙箱目录，
  两种模式行为一致。
- **Secret**：值只写不回读；`Sandbox.create(envs={"K": "${name}"})` 注入。
- **Pause/Resume**：进程树 SIGSTOP/SIGCONT 冻结，不是 VM 内存快照；SDK 2.46
  无独立 resume 方法，paused 沙箱通过 `Sandbox.connect` 自动恢复。
- **Metrics/Logs**：`GET /sandboxes/{id}/metrics`（预留配额 + 沙箱目录用量
  采样）；`GET /sandboxes/{id}/logs`（沙箱生命周期事件 + 命令输出日志：
  envd 把每条命令的 stdout/stderr/PTY 输出按行追加到沙箱目录
  `command-logs.jsonl`，控制面合并返回，格式为 `{timestamp, line}`，命令
  起始行 `> cmd`、stderr 前缀 `stderr:`、结束行 `exit: N`；远程沙箱经
  worker agent 拉取。单命令输出超 1MB 或日志文件超 16MB 自动截断）。
- **MCP**：仅支持本地 stdio base server（`name/command/args/envs`），GitHub
  server 返回 400。网关由 envd 以受限进程启动并随沙箱回收，要求沙箱内有
  Python 3 + `mcp` 包（python 系镜像与纯 Sandlock 满足）。本地部署时
  `get_mcp_url()` 返回云域名，客户端请连接 `http://127.0.0.1:50005/mcp`。
- **Fork/Snapshot**：文件系统级快照（复制沙箱目录 + 元数据），等价官方
  `keep_memory=false` 的冷启动语义——**不保留运行进程/内存/打开的 socket**。
  捕获期间临时冻结沙箱进程树保证文件一致；快照独立于沙箱生命周期
  （kill/TTL 不删快照），`create(template=snapshotID)` 从快照冷启动新沙箱，
  `fork(count=N)` 从同一快照创建 N 个独立沙箱（逐项独立成败）。sandlock
  的内存 checkpoint/restore 因与流式 popen 模型冲突、且 restore 在容器环境
  实测失败，未采用。

## 多节点调度（v3.0 Phase 1）

控制面可管理多个计算节点（Docker 容器节点或物理 Linux 节点，同一套
envd worker + agent 代码），SDK 零修改：

```text
SDK → Control Plane + Envd Gateway :3000（注册表 / 调度 / 准入 / 按
      E2b-Sandbox-Id 路由代理，同端口）
        ├── worker-1（Docker 容器节点）
        ├── worker-2（物理 Linux 节点）
        └── worker-3（…）
```

- **节点注册/心跳**：worker 启动时带 `E2B_CONTROL_PLANE_URL` 与
  `E2B_NODE_ADDRESS`，自动注册并每 5s 心跳；超时节点标记 unhealthy，
  不再调度新沙箱（运行中沙箱不迁移）。
- **调度**：健康过滤 → 镜像亲和（节点已有 rootfs 优先）→ 剩余资源 best-fit
  → 均衡；全局 `E2B_MAX_TOTAL_*` 与节点级配额双重准入，超限 `503`。
- **路由代理**：Gateway 查询控制面路由表（缓存 30s），Connect 流式与
  `/files` 等 HTTP 请求原样透传（`E2b-Sandbox-Id`、`X-Access-Token` 保留）。
- **管理**：`docker compose -f deploy/compose/docker-compose.multinode.yml up` 起
  1 控制面 + 1 gateway + 3 worker；物理节点在另一台 Linux 跑同一
  `python -m envd_service`（agent 自动注册）。
- **镜像仓库**：控制面设置 `E2B_IMAGE_REGISTRY`（如
  `registry.example.com/e2b`）后，`Template.build` 构建完成会把镜像
  push 到仓库，模板的沙箱镜像名改为 `{registry}/{templateID}`；worker
  首次解析镜像时本地 daemon 没有该镜像会先 `docker pull` 再解包 rootfs，
  因此多节点无需手动分发镜像。本地/自建仓库为 HTTP 时，daemon 需把该
  地址加入 insecure-registries（127.0.0.1 默认允许）。未配置
  `E2B_IMAGE_REGISTRY` 时保持单机行为（镜像只在控制面 daemon）。
  **仓库认证**：私有仓库设置 `E2B_IMAGE_REGISTRY_USERNAME` 与
  `E2B_IMAGE_REGISTRY_PASSWORD`（控制面与 worker 都要配，分别用于
  push/pull）。凭据通过 `docker login --password-stdin` 传入，不出现
  在命令行参数里；未配置凭据时按匿名仓库处理。
- **节点迁移**：`POST /sandboxes/{id}/migrate`（可带 `{"nodeID": ...}`
  指定目标节点）把沙箱工作目录从源节点导出（tar.gz）并在目标节点导入，
  记录/路由/配额一并转移，gateway 路由缓存即时失效。运行中的进程不迁移
  （沙箱在目标节点冷启动，文件系统内容保留）；源节点不可达时导出失败返回
  502。迁移带 per-sandbox 锁（Redis 多副本下为 `SETNX` 标记 + TTL，单进程
  为等价内存锁），同一沙箱并发 migrate 第二个请求返回 409，持有者崩溃时
  锁按 TTL 自动过期；失败路径清理标记。迁移先停源节点 runtime（复用
  `DELETE ?keepFiles=true` 的 unregister 路径，进程树被终止）再导出/导入，
  关闭"路由已切换但旧节点进程还活着"的双活窗口；迁移失败会自动在源节点
  重新 provision，沙箱保持可用。控制面通过 `E2B_GATEWAY_URL` 通知 gateway
  失效旧路由。
- **Network API**：`POST /sandboxes` 的 `network` 字段与
  `PUT /sandboxes/{id}/network`（官方 `Sandbox.update_network`，原子替换、
  省略字段清空）已支持：
  - `allowOut` / `denyOut` — 出站白/黑名单（IP/CIDR/域名；`denyOut` 仅
    IP/CIDR，与官方一致），映射到 Sandlock `net_allow`/`net_deny`；
  - `allowPublicTraffic` — 为 true 时 envd HTTP/Connect 端点免
    `X-Access-Token`（仍校验 `E2b-Sandbox-Id`）；
  - `rules` — 注册域名并映射到 Sandlock `http_allow`（80/443 透明 MITM
    按域名 ACL；镜像 rootfs 模式下把临时 CA 拼进每沙箱信任副本并注入
    `SSL_CERT_FILE`，HTTPS 可用）。
  - `egressProxy` — 支持：fork sandlock 的 SOCKS5 **on-behalf** 隧道
    （R12–R14，替代早期 LD_PRELOAD 库）：allow/deny 过滤通过后由
    supervisor 代连用户代理，通配目标走 ATYP=domain 远程 DNS，字面目标
    IPv4/IPv6，RFC 1929 认证，fail closed（代理不可达 → ECONNREFUSED，
    绝不回退直连）；代理端点由 supervisor 拨号、不进沙箱 allowlist。
    控制面校验代理地址必须解析到公网 IPv4（拒绝私网/内网，防 SSRF）。
  - `maskRequestHost` 与 `rules[].transform.headers` — 支持（fork wheel）：
    映射到 sandlock 的 `host_mask`（改写 wire Host，`${PORT}` 替换）与
    `http_inject`（credential 注入，secret 只存 supervisor；字面值落
    supervisor-only 0600 文件，`${e2b.identity.tokens.*}` 映射
    `E2B_IDENTITY_TOKEN_*` env；若沙箱注册了 `iam` 工作负载令牌
    （`Sandbox.create(iam={"tokens": {...}})`），占位符会替换为签发的
    JWT-SVID，签名密钥 `E2B_IAM_SIGNING_KEY`，默认本地开发密钥）。
  - **通配域名**：普通模式（无需 egressProxy / netns）经每沙箱 loopback
    DNS 网关（`127.0.1.x:53`，需一次 `net.ipv4.ip_unprivileged_port_start=0`
    或 `CAP_NET_BIND_SERVICE`）把通配子域解析为合成 IP，connect 由
    supervisor 代连并二次校验（SSRF 护栏拒绝私网/回环），静态/Go 应用
    同样受限。fork 已切到上游 PR 的无 netns 版本（netns-free），
    `E2B_ENABLE_NETNS` 仅作兼容保留、不再生效，全程无需 root /
    `NET_ADMIN`。
  - 注意：需 worker 设置 `E2B_ENABLE_NETWORK=true`（默认 false 时全局
    拒绝出站，网络 API 策略不生效）；普通模式沙箱内 DNS 依赖 Sandlock 的
    hostname pinning，`allowOut` 用域名形式（如 `example.com:443`）最可靠；
    egressProxy 模式下 DNS 由代理侧解析（ATYP=domain）。
- **共享工作目录**：所有节点把 `E2B_WORKSPACE_BASE` 指向同一共享挂载点
  （NFS/CSI），并在控制面设置 `E2B_SHARED_WORKSPACE_ROOT` 后，迁移不再
  打包传输——沙箱目录已在共享存储，只重新 provision 目标节点（runtime +
  卷挂载）、切换记录/路由/配额，且**不删除**源节点上的目录（同一份存储）。
  共享卷（`E2B_SHARED_VOLUME_ROOT`）数据始终不走迁移传输，两种模式下都只
  重建挂载符号链接。注意：共享目录在 NFS 上受 root_squash/uid 映射影响，
  命令文件 IO 走网络；同一沙箱同时只在一个节点运行（路由保证单点）。
- **真实多节点验证**：compose 起来后运行
  `E2B_API_URL=http://127.0.0.1:3100 E2B_SANDBOX_URL=http://127.0.0.1:3100
  python deploy/scripts/multinode_smoke.py`，验证沙箱跨节点分布、经 gateway 的
  命令/文件/stdin、以及 kill 后节点配额释放。worker 容器需要
  `security_opt: [seccomp=unconfined]`（sandlock 需安装嵌套 seccomp 过滤器）
  和 docker CLI（镜像 rootfs 解析）；多节点部署建议
  `E2B_ENABLE_LOCAL_NODE=false` 关闭控制面本机节点，避免抢占调度。

**Phase 1 限制**：卷/快照为控制面本地存储，远程节点创建暂不支持挂载卷
（调度返回明确错误）；控制面单点（Phase 3 才多副本）。

### Phase 2：亲和调度与故障细化

- **卷亲和**：卷归属节点（默认 local），带卷挂载的沙箱强制调度到卷所在
  节点。**共享卷模式**：设置 `E2B_SHARED_VOLUME_ROOT`（所有节点挂载同一
  共享目录，如 NFS/CSI），卷数据对全部节点可见，带卷沙箱可调度到任意
  节点；worker agent 校验挂载目标在共享根内后建立挂载点。未配置共享根时
  保持卷亲和（卷在归属节点本地，多节点部署关闭 local 节点则无法挂卷）。
- **远程快照**：worker agent 提供本地快照捕获/删除 API，远程沙箱的快照在
  节点本地存储；从快照创建/fork 调度到快照所在节点，worker 本地展开。
- **故障细化**：节点 unhealthy 时路由查询返回 502（SDK `is_running()` 得
  False）；新增管理端点 `GET /nodes`、`DELETE /nodes/{id}`。

### Phase 3：Redis 多副本（可选）

设置 `E2B_REDIS_URL` 后，控制面副本共享沙箱/节点注册表，配额预留/释放走
Redis WATCH 事务（原子，跨进程不超用），TTL 扫描跨副本一致；未配置时保持
单进程内存模式。多副本部署启动多个控制面进程指向同一 Redis 即可；记录变更
（node_id、TTL、暂停/恢复）实时写回共享存储，副本间删除/更新立即可见。
真实 Redis e2e：`tests/contract/test_redis_multireplica_e2e.py`（自动起
`redis:7` 容器，验证并发配额原子性、跨副本可见/删除、TTL 回收）。

## Template 本地构建（v2.2）

- `Template().from_dockerfile(...)` + `Template.build(template, name)` 走官方
  API（POST /v3/templates → build trigger → status 轮询），控制面用本地
  Docker daemon 构建镜像并注册为可创建沙箱的模板（`Sandbox.create(template=name)`）。
- 支持 FROM/RUN/ENV/WORKDIR/USER/COPY 步骤。**COPY 文件上下文**：SDK 的
  `Template().copy(src, dest)` / `from_dockerfile` 中的 COPY 会先按内容哈希
  上传 tar 归档（`GET /templates/{id}/files/{hash}` 返回带 token 的上传
  URL，SDK 直接 PUT），控制面解包进 build context 后 `docker build`；
  同一哈希只上传一次。构建产物为 `e2b-local/{templateID}` 镜像，Sandlock
  模式下沙箱在镜像 rootfs 内执行，COPY 的文件在镜像内可见。
- **镜像分发**：配置 `E2B_IMAGE_REGISTRY` 后构建产物会 push 到仓库并把
  模板镜像名切到 `{registry}/{templateID}`（这一步会**落盘**到模板记录），
  worker 节点按需从仓库拉取（见多节点调度一节）。**改了 registry 之后，
  之前构建的模板仍指向旧地址，需要重新构建。**
- **公共镜像源**：`E2B_REGISTRY_MIRRORS=host=mirrorA|mirrorB,...`（如
  `registry-1.docker.io=docker.m.daocloud.io`）让 worker 经镜像源解析公共镜像，
  避免 Docker Hub 匿名配额（429）拖垮建沙箱；origin host 始终作为最后一个端点
  兜底，`E2B_IMAGE_MANIFEST_TTL_S`（默认 60s）再压一层查询频率。
- **不配 registry 的单机形态**：没有可 push 的目标，构建改为把
  **OCI layout tar** 导出到 `E2B_IMAGE_CACHE_DIR/_oci/`，本节点的 worker
  从该 tar 解析 rootfs（解析与 `warm` 探测都不需要 registry）。这条路径只
  覆盖"建镜像的这台节点"——远端 worker 仍然必须有 `E2B_IMAGE_REGISTRY`。`E2B_IMAGE_CACHE_DIR` 控制 rootfs 解包缓存位置，
  建议指向节点本地盘（默认 `tmp/sandboxes/_images` 为相对 cwd 的本地
  路径），与共享的 `E2B_WORKSPACE_BASE` 解耦——**workspace 只存用户文件，
  镜像 rootfs 始终在节点本地存储**。缓存目录名包含镜像 digest
  （`{image}-{sha256 前缀}`），基础镜像 tag 更新（如 `python:3.14-slim`
  出新版）后自动落到新目录，不会误用旧 rootfs；旧 digest 目录保留，
  需要时手动清理 `E2B_IMAGE_CACHE_DIR` 下的历史目录。
