# E2B-Sandlock 网关：完整方案文档

**文档版本**：v2.0.0
**最后更新**：2026-09-06（M4 D6：`max_processes` 整箱默认 64→256）
**状态**：设计就绪，待实现

## 1. 目标与兼容定义

### 1.1 项目目标

本项目提供本地 E2B 兼容层：

- 官方 E2B Python SDK / JavaScript SDK 零代码修改。
- 控制面复刻官方 Sandbox REST API。
- envd 复刻官方 Connect-RPC 与文件 HTTP 契约。
- 底层使用 Sandlock（Landlock + seccomp-bpf + seccomp user notification）执行用户代码。
- 提供完整测试体系，用官方 SDK 和原始 HTTP/Connect 请求验证兼容性。

### 1.2 兼容基线

| 维度 | 基线 |
|------|------|
| Python SDK | `e2b==2.46.1`，同步与异步 Sandbox API |
| JS/TS SDK | `e2b@2.46.1`，Node.js |
| 控制面契约 | 官方 `spec/openapi.yml` 中沙箱相关路径 |
| envd RPC 契约 | 官方 `spec/envd/process/process.proto`、`spec/envd/filesystem/filesystem.proto` |
| envd HTTP 契约 | 官方 `spec/envd/envd.yaml` |
| Sandlock Python SDK | `sandlock==0.8.6` |
| 客户端改造 | 无代码修改，只允许设置 `E2B_API_URL`、`E2B_SANDBOX_URL`、`E2B_API_KEY` |
| envd 版本 | 对外返回 `0.6.4-sandlock` |

### 1.3 支持范围

| 模块 | 说明 |
|------|------|
| Sandbox 生命周期 | `create`、`connect`、`list`、`get_info`、`kill`、`set_timeout`、`is_running` |
| Commands | 前台/后台 `run`、`list`、`connect`、`kill`、`send_stdin`、`close_stdin` |
| PTY | `create`、`send_input`、`resize`、`kill`，需先通过 Envd PTY 适配 spike |
| Filesystem | `read`、`write`、`list`、`exists`、`get_info`、`remove`、`rename`、`make_dir`、`watch_dir` |
| Envd HTTP | `/health`、`/envs`、`/metrics`、`/files` |
| 认证 | 控制面 `X-API-Key`；envd `X-Access-Token` 与 `Authorization: Basic` |

### 1.4 明确不支持

Template 构建、Volume、Secret、Snapshot、Network 动态更新（header 改写部分）、IAM、MCP、Events/Logs、Pause/Resume/Fork、多节点调度。

不支持 API 必须返回官方 `Error` JSON，禁止返回假成功。

## 2. 系统架构

### 2.1 双服务架构

SDK 先访问控制面创建沙箱，再直连 envd 执行命令和文件操作。

```text
SDK (Python 2.46.1 / JS 2.46.1)
  |
  | E2B_API_URL
  v
Control Plane :3000
  官方 Sandbox OpenAPI
  沙箱注册表 / TTL / 认证 / Runtime 生命周期
  |
  | E2B_SANDBOX_URL
  v
Envd Service :49983
  Connect-RPC: process.Process / filesystem.Filesystem
  HTTP: /files /health /envs /metrics
  按 E2b-Sandbox-Id 路由
  |
  v
Runtime Manager / SandlockExecutor
  Landlock + seccomp-bpf + seccomp user notification
```

### 2.2 创建沙箱流程

1. SDK 发送 `POST /sandboxes` 到 Control Plane。
2. Control Plane 校验 API Key、模板和请求字段。
3. Control Plane 分配 `sandboxID`、`clientID`、`envdAccessToken`，创建工作目录。
4. Runtime Manager 生成 Sandlock 策略并启动 runtime。
5. Control Plane 返回官方 `Sandbox` JSON。
6. SDK 后续请求携带 `E2b-Sandbox-Id`、`E2b-Sandbox-Port`、`X-Access-Token`。

### 2.3 命令执行流程

1. SDK 发送 `POST /process.Process/Start` 到 Envd Service。
2. Envd Service 按 `E2b-Sandbox-Id` 定位 runtime。
3. SandlockExecutor 启动受限子进程。
4. envd 返回服务器流：`start` 事件、`stdout/stderr/pty` 数据事件、`end` 事件。
5. SDK 封装为 `CommandResult` 或 `CommandHandle`。

## 3. 环境要求

| 项 | 要求 |
|----|------|
| Linux 内核 | >= 6.12，完整启用 Landlock ABI v6；PyPI 包最低要求 6.7 |
| Python | 3.11 |
| Node.js | 20 LTS 或 22 LTS |
| Sandlock | `sandlock==0.8.6`，无 Root、无 cgroup、无容器依赖 |
| 工作目录 | 默认项目内 `tmp/sandboxes/` |
| 测试运行环境 | macOS 仅运行不依赖 Sandlock 的单元/契约测试；真实执行测试必须在 Linux 6.12+ 容器或 Linux VM 内 |
| Docker daemon | 用于启动 Linux test runner 容器，并支持 Sandlock `--image` 镜像解析 |

运行前检查：

```bash
uname -r
grep CONFIG_LANDLOCK /boot/config-$(uname -r)
cat /proc/self/attr/landlock
python3 -c "import sandlock; print(sandlock.landlock_abi_version())"
```

## 4. Control Plane 契约

### 4.1 端点

| 方法 | 路径 | 官方 SDK 对应 | 成功响应 |
|------|------|---------------|----------|
| `POST` | `/sandboxes` | `Sandbox.create()` | `201 Sandbox` |
| `GET` | `/sandboxes` | 旧版列表 | `200 [ListedSandbox]` |
| `GET` | `/v2/sandboxes` | `Sandbox.list()` | `200 [ListedSandbox]` + `X-Next-Token` |
| `GET` | `/sandboxes/{sandboxID}` | `Sandbox.getInfo()` | `200 SandboxDetail` |
| `DELETE` | `/sandboxes/{sandboxID}` | `Sandbox.kill()` | `204` |
| `POST` | `/sandboxes/{sandboxID}/connect` | `Sandbox.connect()` | `200 Sandbox` |
| `POST` | `/sandboxes/{sandboxID}/timeout` | `Sandbox.setTimeout()` | `204` |

### 4.2 认证与错误

- 认证头：`X-API-Key: <key>`。
- 未认证返回 `401`。
- 官方错误体：

```json
{"code": 404, "message": "Sandbox sbx_xxx not found"}
```

- `kill` 遇 `404` 必须让 SDK 返回 `False`。
- `get_info`、`connect`、`set_timeout` 遇 `404` 必须抛出官方对应异常。

### 4.3 创建沙箱

请求：

```json
{
  "templateID": "base",
  "timeout": 300,
  "metadata": {"user": "alice"},
  "envVars": {"MY_VAR": "value"},
  "secure": true,
  "allow_internet_access": false
}
```

字段规则：

| 字段 | 规则 |
|------|------|
| `templateID` | 必填，官方 SDK 只传模板 ID/名称，不支持直接传镜像；`base` 映射到 `E2B_BASE_IMAGE`，其他模板由 `E2B_TEMPLATE_IMAGES` 注册 |
| `timeout` | TTL 秒数，官方 OpenAPI 默认 `15`，SDK 默认显式传 `300` |
| `metadata` | 保存并支持列表过滤 |
| `envVars` | 注入 runtime |
| `secure` | 始终启用并返回 `envdAccessToken` |
| `allow_internet_access` | 映射到 Sandlock 网络策略 |
| `image`（非官方字段） | 不支持，返回 `400 Error` |
| `network` | 支持 `allowOut`/`denyOut`/`allowPublicTraffic`/`rules`（域名级 HTTP ACL）/`maskRequestHost`/`rules.transform.headers`（header 注入）/`egressProxy`（SOCKS5 隧道，含 username/password） |
| `iam` | 支持：`{tokens: {name: {audience, tokenType}}}`；`${e2b.identity.tokens.<name>}` 占位符在代理内替换为签发的 JWT-SVID（HS256，`E2B_IAM_SIGNING_KEY`，默认本地开发密钥） |
| `mcp` | 支持：base stdio server（command）；GitHub MCP 返回 `400` |
| `volumeMounts` | 支持：挂载到 rootfs 内 `/home/user/<path>`（或工作区符号链接） |

模板与基础镜像映射：

| 模板 ID | 镜像来源 |
|----------|----------|
| `base` | 总是有效；配置 `E2B_BASE_IMAGE` 时使用该镜像，未配置时纯 Sandlock |
| 其他模板 | `E2B_TEMPLATE_IMAGES` JSON 映射，例如 `{"python3.12": "python:3.12-slim", "node22": "node:22-slim"}` |

解析顺序：`templateID` 精确命中 `E2B_TEMPLATE_IMAGES`，否则使用 `base`；未知模板返回 `400 Error`。模板解析到基础镜像时启用容器隔离，未解析到镜像时使用纯 Sandlock。

响应：

```json
{
  "templateID": "base",
  "sandboxID": "sbx_1f8d3c9a",
  "clientID": "cli_4b7a2e",
  "envdVersion": "0.6.4-sandlock",
  "envdAccessToken": "tok_9f3a",
  "trafficAccessToken": null,
  "domain": "localhost"
}
```

### 4.4 列表与详情

`GET /v2/sandboxes` 参数：

| 参数 | 说明 |
|------|------|
| `metadata` | URL 编码的 `key=value&key2=value2` |
| `state` | `running`；`paused` 返回空数组 |
| `nextToken` | 分页游标 |
| `limit` | 1-100 |
| `order` | `asc`/`desc` |
| `startedAfter` | ISO 时间 |
| `template` | 模板名 |

分页响应通过 `X-Next-Token` 头返回下一页游标，无下一页时不返回该头。

`ListedSandbox` 示例：

```json
{
  "templateID": "base",
  "alias": "base",
  "sandboxID": "sbx_1f8d3c9a",
  "clientID": "cli_4b7a2e",
  "startedAt": "2026-08-28T10:00:00.000Z",
  "endAt": "2026-08-28T10:05:00.000Z",
  "cpuCount": 1,
  "memoryMB": 1024,
  "diskSizeMB": 1024,
  "metadata": {"user": "alice"},
  "state": "running",
  "envdVersion": "0.6.4-sandlock"
}
```

`SandboxDetail` 在此基础上增加 `envdAccessToken`、`allowInternetAccess`、`domain`、`lifecycle`、`network`、`volumeMounts`。

### 4.5 连接、续期与删除

`POST /sandboxes/{sandboxID}/connect`：

```json
{"timeout": 300}
```

响应为 `Sandbox`，TTL 重置为 `now + timeout`。

`POST /sandboxes/{sandboxID}/timeout`：

```json
{"timeout": 600}
```

成功返回 `204`。

`DELETE /sandboxes/{sandboxID}`：

- 成功返回 `204`。
- 不存在返回 `404 Error`。

禁止推荐 `E2B_DEBUG=true` 作为接入方式，因为官方 SDK 在 debug 模式会跳过 kill、setTimeout、metrics。

## 5. Envd 契约

### 5.1 路由与认证

| Header | 来源 | 用途 |
|--------|------|------|
| `E2b-Sandbox-Id` | SDK 自动 | 路由沙箱 |
| `E2b-Sandbox-Port` | SDK 自动 | 当前为 `49983` |
| `X-Access-Token` | 创建响应 | 校验访问权限 |
| `Authorization: Basic <base64(user:)>` | 用户指定时 | 指定用户 |

token 不匹配时返回 Connect `unauthenticated` / HTTP `401`。

### 5.2 HTTP 端点

| 方法 | 路径 | 成功响应 | 说明 |
|------|------|----------|------|
| `GET` | `/health` | `204` | 健康探测 |
| `GET` | `/envs` | `200` JSON | 环境变量 |
| `GET` | `/metrics` | `200` JSON | CPU/内存/磁盘 |
| `GET` | `/files` | `200 application/octet-stream` | 下载 |
| `POST` | `/files` | `200 JSON 数组` | 上传 |
| `POST` | `/init` | `204` | 初始化入口 |

`GET /files` 参数：`path`、`username`、`signature`、`signature_expiration`。

`POST /files` 支持 multipart 和 `application/octet-stream` 单文件上传。多文件使用多个 `file` 字段且不传 `path`。

上传成功响应：

```json
[
  {
    "name": "script.py",
    "type": "file",
    "path": "workspace/script.py",
    "metadata": null
  }
]
```

### 5.3 Connect-RPC 方法

Process：

| 方法 | 类型 | 用途 |
|------|------|------|
| `List` | Unary | 列出进程 |
| `Start` | Server streaming | 启动命令/PTY |
| `Connect` | Server streaming | 重连命令/PTY |
| `Update` | Unary | PTY resize |
| `SendInput` | Unary | 发送 stdin/PTY 数据 |
| `SendSignal` | Unary | SIGTERM/SIGKILL |
| `CloseStdin` | Unary | EOF |
| `StreamInput` | Bidirectional streaming | 官方 proto 保留 |

Filesystem：

| 方法 | 类型 | 用途 |
|------|------|------|
| `Stat` | Unary | 详情 |
| `MakeDir` | Unary | 递归创建目录 |
| `Move` | Unary | 重命名/移动 |
| `Remove` | Unary | 删除 |
| `ListDir` | Unary | 列出目录 |
| `WatchDir` | Server streaming | 目录事件 |
| `CreateWatcher` / `GetWatcherEvents` / `RemoveWatcher` | Unary | 旧版 watch |

### 5.4 Connect 协议

官方 SDK 使用 JSON 编解码。实现要求：

- Unary：`POST /{package}.{Service}/{Method}`，`Content-Type: application/json`。
- Server streaming：`POST /{package}.{Service}/{Method}`，`Content-Type: application/connect+json`。
- 流式请求体：`1 byte flags + 4 byte big-endian length + JSON`。
- 流式响应：每个消息同样使用 envelope；正常消息 `flags=0`，最终 `EndStreamResponse` `flags=2`。
- 成功最终消息可以是 `{}`；失败必须为 `{"error":{"code":"...","message":"..."}}`。
- 流式响应始终 HTTP `200`。

### 5.5 Process 事件序列

Start 请求：

```json
{
  "process": {
    "cmd": "/bin/bash",
    "args": ["-l", "-c", "python3 -c 'print(1+1)'"],
    "envs": {"PATH": "/usr/bin:/bin"},
    "cwd": "/home/user"
  },
  "stdin": false
}
```

事件序列：

```json
{"event": {"start": {"pid": 42}}}
{"event": {"data": {"stdout": "2\n"}}}
{"event": {"end": {"exitCode": 0, "exited": true, "status": "exited", "error": null}}}
```

非零退出码时 SDK `wait()` 抛出 `CommandExitException`，必须保留 `exit_code`、`stdout`、`stderr`。

### 5.6 Filesystem RPC 语义

- `MakeDir` 目录已存在返回 Connect `already_exists`。
- `Stat` 不存在返回 Connect `not_found`。
- `ListDir` 的 `depth=0` 不限深度。
- `WatchDir` 首条消息必须为 `{"start": {}}`。
- 事件类型：`create`、`write`、`remove`、`rename`、`chmod`。

## 6. 双服务实现设计

### 6.1 模块结构

```text
e2b-sandlock-gateway/
├── control_plane/
│   ├── app.py
│   ├── api/sandboxes.py
│   ├── registry/manager.py
│   ├── registry/ttl.py
│   ├── auth.py
│   └── config.py
├── envd_service/
│   ├── app.py
│   ├── connect/codec.py
│   ├── connect/router.py
│   ├── process/manager.py
│   ├── process/events.py
│   ├── filesystem/ops.py
│   ├── filesystem/watch.py
│   ├── http/files.py
│   ├── http/health.py
│   └── runtime/sandlock_executor.py
├── deploy/docker/Dockerfile.test-runner
├── deploy/compose/docker-compose.test.yml
├── tests/
│   ├── unit/
│   ├── contract/
│   ├── sdk/
│   ├── security/
│   └── perf/
└── tmp/sandboxes/
```

### 6.2 Control Plane 职责

- 解析官方 OpenAPI 请求/响应。
- 维护内存沙箱注册表。
- 生成随机 `sandboxID`、`clientID`、`envdAccessToken`。
- 管理 TTL 与到期回收。
- 创建/销毁 Sandlock runtime。
- 创建沙箱前执行总资源准入检查，超限返回 `503 Error` 且不启动 runtime。
- 不暴露命令或文件路由。

### 6.3 Envd Service 职责

- 按 `E2b-Sandbox-Id` 路由。
- 校验 `X-Access-Token`。
- 实现 Connect envelope、错误映射和服务器流。
- 实现 `/files`、`/health`、`/envs`、`/metrics`。
- 维护进程表和 PTY。

### 6.4 Sandlock 策略映射

| 配置 | 策略 |
|------|------|
| `allow_internet_access=false` | 禁止出网 |
| `allow_internet_access=true` | 仅允许配置白名单 |
| `fs_readable` | `/usr`、`/lib`、`/bin` |
| `fs_writable` | 仅沙箱工作目录 |
| `fs_denied` | `/proc/kcore`、`/sys` |
| `max_memory` | `max_memory="1024M"`，seccomp user notification 内存跟踪（updated 2026-09-06: per-sandbox default 1024, FUP3） |
| `max_cpu` | `max_cpu=100`，SIGSTOP/SIGCONT 按单核百分比限流 |
| `max_processes` | `max_processes=256`，seccomp user notification 并发进程计数（M4 D6 whole-box semantics, 2026-09-06） |
| `max_open_files` | `max_open_files`，RLIMIT_NOFILE |
| `max_disk` | `max_disk`，仅作用于 COW storage 配额 |
| 默认用户 | 非 Root 用户 |

### 6.4.1 Sandlock API 对齐审计

> **superseded（2026-09-06，M4）**：本节描述的是 pre-M4 每命令一个 Sandlock 实例的
> 架构假设；M4 起 E2B 每沙箱持有一只 exec-only `SandboxInstance`，命令与 MCP 网关都经
> `instance.exec()`（整箱预算、`update_network` D4=A、PTY 走 `ExecStdio.PTY`）。
> 下文逐条仅作历史对齐审计记录，当前语义以 `docs/HANDOFF.md`「M4 收口」为准。

当前 spec 必须按 `sandlock==0.8.6` 的真实 API 调整，不能沿用“cgroup v2 + 每沙箱一个常驻进程”的假设：

- 一个 `Sandbox` 实例同一时刻只能运行一个命令。Envd Process Manager 必须为每个命令创建独立 Sandlock 实例。
- E2B 沙箱内多个命令需要共享持久目录。默认不使用 Sandlock `workdir` COW，而是 `fs_writable=[sandbox_dir]`，避免多个实例并发写 COW 层互相冲突。
- `max_disk` 只限制 COW storage。非 COW 模式下，磁盘配额只能作为创建时准入预留；真正的写盘硬限制需要额外配置宿主文件系统 quota。
- Sandlock 没有原生 PTY API。PTY 必须由 Envd 层用 `run_interactive` + 宿主 PTY 适配器实现，并在验收前通过 spike；未通过前不得宣称 PTY 完全兼容。
- `WatchDir` 与 `/metrics` 由 Envd Service 基于文件系统和 `/proc` 实现，不映射到 Sandlock API。
- 资源限制由 seccomp user notification 与 SIGSTOP/SIGCONT 实现，不依赖 cgroup，因此不需要 Root 或 cgroup delegation。

### 6.4.2 CLI 工具安装

Sandlock 不提供“安装工具”的内置能力，但可以把安装命令当作普通受限命令执行。是否成功取决于：

- 安装目标路径是否在 `fs_writable` 内。
- 包管理器运行时需要的缓存、配置、临时路径是否可读可写。
- 下载依赖时是否配置了对应 `net_allow` 规则。
- 安装产物是否写回 E2B 沙箱持久目录。

推荐的无 Root 安装方式：

| 工具 | 示例 |
|------|------|
| Python | `pip install --user` 或 `pip install --prefix=<sandbox_dir>/python` |
| npm | `npm install -g --prefix <sandbox_dir>/npm` |
| Go | `GOBIN=<sandbox_dir>/bin go install ...` |
| Cargo | `cargo install --root <sandbox_dir>/cargo` |
| 二进制 | 下载 tar.gz/zip 到沙箱目录后解压 |

需要联网时，Control Plane 必须把包仓库加入 `net_allow`，例如：

```text
files.pythonhosted.org:443
pypi.org:443
registry.npmjs.org:443
proxy.golang.org:443
static.crates.io:443
```

系统级包管理器（`apt-get`、`dnf`、`yum`）需要写入 `/usr`、`/var/lib`、`/var/cache` 等系统路径，默认无 Root 时不承诺可用。`uid=0/gid=0` 只提供用户命名空间内的假 Root，不能保证 post-install 脚本需要的全部内核能力，因此不作为默认能力。

持久化规则：

- 默认非 COW 模式下，安装到 `sandbox_dir` 的文件在同一 E2B 沙箱内的后续命令可见。
- 若启用 Sandlock `workdir` COW，则必须设置 `on_exit=commit`，且安装目标位于 `workdir` 下；否则安装产物会被丢弃。

### 6.4.3 真实执行测试容器与镜像 rootfs

Sandlock 依赖 Linux 的 Landlock、seccomp-bpf 和 seccomp user notification，macOS 本机不能直接运行。真实执行测试必须在外层 Linux test runner 容器内启动 Control Plane、Envd Service 和 Sandlock，再由 Sandlock 把容器镜像解析为每条命令的 rootfs：

```text
macOS 开发机
  └── Docker test runner（Linux 6.12+，安装 sandlock==0.8.6）
        └── Control Plane :3000 + Envd Service :49983
              └── Sandlock --image python:3.11-slim
                    └── chroot 到镜像 rootfs
                          └── Landlock + seccomp + seccomp notif
                                └── 执行 python3 / node / sh 等真实命令
```

实现要求：

- 模板配置了基础镜像时，测试和运行时统一使用该镜像；`base` 使用 `E2B_BASE_IMAGE`，其他模板使用 `E2B_TEMPLATE_IMAGES`。
- Control Plane 创建沙箱时按 `templateID` -> `E2B_TEMPLATE_IMAGES` -> `E2B_BASE_IMAGE` 解析镜像名，并让 Envd/Runtime 使用 Sandlock 镜像 rootfs 模式；SDK 请求本身不接受 `image` 字段。
- 模板未配置基础镜像时，该模板使用纯 Sandlock 模式，不创建外层容器。
- 项目提供 `deploy/docker/Dockerfile.test-runner` 和 `deploy/compose/docker-compose.test.yml`，test runner 基于 Linux Python 3.11，安装 `sandlock==0.8.6`，挂载源码与 `tmp/sandboxes/`。
- macOS 本机通过 Docker 启动 test runner；Linux CI 可直接运行同一测试命令，避免两套测试路径。
- 镜像解析走 Docker daemon API；test runner 必须有 Docker daemon 访问权限，但沙箱执行不依赖容器运行时。
- 镜像 rootfs 内的命令仍受 Landlock 文件系统规则、seccomp 过滤、网络规则和资源限制约束。
- 集成测试必须验证命令运行在镜像 rootfs 内，例如读取镜像内安装的运行时路径，而不是宿主路径。
- 启动 test runner 前必须检查 `sandlock.landlock_abi_version() >= 6`；macOS Docker Desktop 的 Linux VM 内核不满足时，不得将本机作为 Sandlock 验收环境。

### 6.4.4 容器增强隔离（由模板基础镜像决定）

默认模式是纯 Sandlock 进程级隔离。当某个模板配置了基础镜像时，该模板的每个 E2B 沙箱 runtime 自动放进独立容器，再在容器内运行 Sandlock：

```text
Control Plane
  └── 创建容器（Docker/containerd）
        └── 独立 PID/UTS/mount/network namespace + cgroup v2
              └── Envd runtime
                    └── Sandlock（Landlock + seccomp + seccomp notif）
                          └── 执行用户命令
```

容器层补充的能力：

- PID namespace 隔离进程可见性。
- mount namespace 隔离宿主文件系统。
- network namespace 隔离网络栈。
- cgroup v2 提供 memory、cpu、pids、磁盘硬限制。
- 镜像 rootfs 提供更接近 VM 的文件系统布局。
- 容器内可执行部分系统级包安装，但仍不等于真实 Root/完整内核能力。

仍然不能达到 VM 级隔离：

- 与宿主共享同一个内核，内核漏洞仍可能逃逸。
- 容器 runtime、Root/daemon 和 capabilities 引入额外攻击面。
- 启动延迟和资源占用高于纯 Sandlock。

实现要求：

- 模板配置基础镜像后，Control Plane 从 `templateID` 解析出的基础镜像创建每沙箱容器，挂载 `sandbox_dir` 和 envd 通信入口；SDK 仍只传 `templateID`。
- 容器资源限制与 `E2B_DEFAULT_*`/`E2B_MAX_TOTAL_*` 保持一致。
- 容器内仍必须运行 `sandlock==0.8.6`，禁止只依赖容器默认隔离。
- 未配置基础镜像的模板保持“无 Root、无 cgroup、无容器依赖”的纯 Sandlock 模式。
- 同一 Control Plane 可以同时运行两种模板：有镜像的模板走容器隔离，无镜像的模板走纯 Sandlock。

功能完整性优先，不得为性能放宽隔离。

### 6.5 沙箱隔离

- 每个沙箱独立工作目录。
- 用户代码只允许写工作目录。
- kill 时终止整个进程树。
- 命令超时发送 `end` 事件并 SIGKILL 进程组。
- TTL 到期清理目录、进程和注册表。
- state 只有 `running`。

### 6.6 总资源上限与准入控制

Control Plane 必须同时限制沙箱数量和宿主总资源，避免 `E2B_MAX_SANDBOXES` 数量达标但聚合资源超过宿主能力。

单沙箱默认配额：

| 资源 | 默认值 |
|------|--------|
| 内存 | `E2B_DEFAULT_MEMORY_MB=1024`（updated 2026-09-06: per-sandbox default 1024, FUP3） |
| CPU | `E2B_DEFAULT_CPU_PERCENT=100`，映射 `max_cpu=100` |
| 磁盘 | `E2B_DEFAULT_DISK_MB=1024`，非 COW 时为准入预留 |
| 并发进程 | `E2B_DEFAULT_MAX_PROCESSES=256`，映射 `max_processes=256`（M4 D6 whole-box semantics, 2026-09-06） |

宿主总上限：

| 资源 | 默认值 |
|------|--------|
| 总内存 | `E2B_MAX_TOTAL_MEMORY_MB=8192` |
| 总 CPU | `E2B_MAX_TOTAL_CPU_PERCENT=400` |
| 总磁盘 | `E2B_MAX_TOTAL_DISK_MB=10240` |
| 总进程 | `E2B_MAX_TOTAL_PROCESSES=2048` |

准入算法使用预留配额，而不是等待实际用量超限：

```text
可创建 =
  当前沙箱数 < E2B_MAX_SANDBOXES
  and 已预留内存 + 单沙箱内存 <= E2B_MAX_TOTAL_MEMORY_MB
  and 已预留 CPU + 单沙箱 CPU <= E2B_MAX_TOTAL_CPU_PERCENT
  and 已预留磁盘 + 单沙箱磁盘 <= E2B_MAX_TOTAL_DISK_MB
  and 已预留进程数 + 单沙箱最大进程数 <= E2B_MAX_TOTAL_PROCESSES
```

- 创建成功时立即预留配额。
- kill 或 TTL 回收后立即释放配额。
- 任一资源不足时返回 `503`：

```json
{"code": 503, "message": "No resources available"}
```

- 任一总上限设置为 `0` 表示关闭该维度限制。

## 7. 部署与配置

### 7.1 SDK 接入

Python：

```python
import os
os.environ["E2B_API_URL"] = "http://localhost:3000"
os.environ["E2B_SANDBOX_URL"] = "http://localhost:49983"
os.environ["E2B_API_KEY"] = "local-key"

from e2b import Sandbox

sandbox = Sandbox()
result = sandbox.commands.run("python3 -c 'print(1+1)'")
assert result.stdout == "2\n"
assert result.exit_code == 0
sandbox.kill()
```

JS/TS：

```typescript
import { Sandbox } from 'e2b'

const sandbox = await Sandbox.create({
  apiUrl: 'http://localhost:3000',
  sandboxUrl: 'http://localhost:49983',
  apiKey: 'local-key',
})

const result = await sandbox.commands.run('python3 -c "print(1+1)"')
console.log(result.stdout)
await sandbox.kill()
```

### 7.2 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `E2B_API_KEY` / `E2B_API_KEYS` | `local-key` | 控制面 API Key |
| `E2B_CONTROL_PLANE_PORT` | `3000` | 控制面端口 |
| `E2B_ENVD_PORT` | `49983` | envd 端口 |
| `E2B_WORKSPACE_BASE` | `tmp/sandboxes` | 工作目录 |
| `E2B_MAX_SANDBOXES` | `100` | 最大并发 |
| `E2B_DEFAULT_TIMEOUT` | `300` | 默认 TTL |
| `E2B_MAX_COMMAND_TIMEOUT` | `3600` | 命令最大超时 |
| `E2B_DEFAULT_MEMORY_MB` | `1024` | 默认内存（updated 2026-09-06: per-sandbox default 1024, FUP3） |
| `E2B_DEFAULT_CPU_PERCENT` | `100` | 单沙箱 CPU |
| `E2B_DEFAULT_DISK_MB` | `1024` | 单沙箱磁盘 |
| `E2B_DEFAULT_MAX_PROCESSES` | `256` | 单沙箱最大并发进程数（M4 D6 whole-box semantics, 2026-09-06） |
| `E2B_BASE_IMAGE` | 未配置 | `base` 模板的基础镜像；配置后该模板启用容器隔离 |
| `E2B_TEMPLATE_IMAGES` | `{}` | 模板 ID 到基础镜像的 JSON 映射；条目存在即该模板启用容器隔离 |
| `E2B_MAX_TOTAL_MEMORY_MB` | `8192` | 宿主总内存上限 |
| `E2B_MAX_TOTAL_CPU_PERCENT` | `400` | 宿主总 CPU 上限 |
| `E2B_MAX_TOTAL_DISK_MB` | `10240` | 宿主总磁盘上限 |
| `E2B_MAX_TOTAL_PROCESSES` | `2048` | 宿主总并发进程上限 |
| `E2B_ENABLE_NETWORK` | `false` | 是否允许网络 |
| `E2B_LOG_LEVEL` | `INFO` | 日志级别 |

### 7.3 Docker Compose

```yaml
services:
  control-plane:
    build:
      context: ../..
      dockerfile: deploy/docker/Dockerfile.control-plane
    environment:
      E2B_API_KEYS: local-key
      E2B_WORKSPACE_BASE: /var/lib/e2b-sandboxes
      E2B_MAX_SANDBOXES: 100
      E2B_BASE_IMAGE: python:3.11-slim
      E2B_TEMPLATE_IMAGES: '{"python3.12": "python:3.12-slim", "node22": "node:22-slim"}'
      E2B_DEFAULT_MEMORY_MB: 1024  # updated 2026-09-06: per-sandbox default 1024, FUP3
      E2B_DEFAULT_CPU_PERCENT: 100
      E2B_DEFAULT_DISK_MB: 1024
      E2B_DEFAULT_MAX_PROCESSES: 256  # M4 D6 whole-box semantics, 2026-09-06
      E2B_MAX_TOTAL_MEMORY_MB: 8192
      E2B_MAX_TOTAL_CPU_PERCENT: 400
      E2B_MAX_TOTAL_DISK_MB: 10240
      E2B_MAX_TOTAL_PROCESSES: 2048
    ports:
      - "3000:3000"
    volumes:
      - sandbox-data:/var/lib/e2b-sandboxes

  envd:
    build:
      context: ../..
      dockerfile: deploy/docker/Dockerfile.envd
    environment:
      E2B_WORKSPACE_BASE: /var/lib/e2b-sandboxes
    ports:
      - "49983:49983"
    volumes:
      - sandbox-data:/var/lib/e2b-sandboxes
    cap_add:
      - SYS_ADMIN
    privileged: false

volumes:
  sandbox-data:
```

### 7.4 生产部署

- 两个服务使用独立 systemd unit。
- Envd Service 可水平扩展，按 `E2b-Sandbox-Id` 哈希路由。
- 日志输出 JSON Lines。

## 8. 完整测试方案

### 8.1 测试原则

- 覆盖外部可观察行为。
- 断言必须精确匹配，禁止 `toContain`、`includes`、`assertContains`。
- 禁止 SKIP 或过滤错误输出。
- 临时数据只放项目 `tmp/`。
- 性能测试必须记录 profile。
- SDK、安全、性能真实执行测试必须在 Linux 6.12+ 的 test runner 容器或 Linux VM 内执行；macOS 本机只跑不依赖 Sandlock 的单元/契约测试。

### 8.2 测试分层

| 层 | 工具 | 目的 |
|----|------|------|
| L1 单元 | pytest / vitest | 注册表、TTL、策略映射、Connect envelope、路径安全 |
| L2 契约 | raw httpx / fetch | 官方 HTTP 与 Connect wire 格式 |
| L3 SDK | 官方 `e2b==2.46.1` | 零改造兼容 |
| L4 安全 | pytest + subprocess | 隔离与资源限制 |
| L5 性能 | pytest + py-spy/pprof | 延迟、吞吐、profile |

### 8.3 单元测试

| 文件 | 覆盖 |
|------|------|
| `test_sandbox_registry.py` | 创建、查找、删除、最大并发 |
| `test_total_resource_admission.py` | 总内存/CPU/磁盘/进程准入、释放、`503` |
| `test_template_image_mapping.py` | `templateID` 到基础镜像解析、未知模板、`image` 字段拒绝 |
| `test_ttl.py` | TTL 设置、续期、回收 |
| `test_connect_envelope.py` | flags、长度、JSON、错误 envelope |
| `test_policy_mapping.py` | E2B 请求到 `sandlock==0.8.6` API 映射 |
| `test_path_safety.py` | 路径穿越 |
| `test_filesystem_ops.py` | stat/list/move/remove/makedir |

```python
assert envelope.flags == 0
assert envelope.length == len(payload)
assert envelope.decode() == {"event": {"start": {"pid": 42}}}
```

### 8.4 契约测试

| 测试 | 精确断言 |
|------|----------|
| 创建沙箱 | `status_code == 201`，字段非空 |
| 按模板镜像创建沙箱 | `templateID` 命中 `E2B_TEMPLATE_IMAGES`，`status_code == 201` |
| 非官方 `image` 字段 | `status_code == 400`，错误体精确匹配 |
| 错误认证 | `status_code == 401`，错误体精确匹配 |
| 列表分页 | 第一页 `X-Next-Token` 精确，第二页无该头 |
| 删除不存在沙箱 | `status_code == 404`，`body["code"] == 404` |
| 文件读取 | `body == b"print(1+1)\n"` |
| 文件上传 | JSON 精确等于单元素 EntryInfo |
| `/health` | `status_code == 204`，`body == b""` |
| Start 流 | 首消息为 start，最终 flags 为 `2` |

### 8.5 Python SDK 测试

`test_sandbox.py`：

```python
@pytest.mark.asyncio
async def test_async_lifecycle():
    sandbox = await AsyncSandbox.create()
    try:
        assert (await sandbox.is_running()) is True
        info = await sandbox.get_info()
        assert info.sandbox_id == sandbox.sandbox_id
        assert info.state == "running"
    finally:
        assert await sandbox.kill() is True
```

`test_commands.py`：

```python
def test_command_result_is_exact(sandbox):
    result = sandbox.commands.run("echo hello")
    assert result.stdout == "hello\n"
    assert result.stderr == ""
    assert result.exit_code == 0

def test_command_exit_code_is_exact(sandbox):
    with pytest.raises(Exception) as exc:
        sandbox.commands.run("exit 7")
    assert exc.value.exit_code == 7
```

`test_stdin.py`：

```python
def test_stdin_roundtrip(sandbox):
    proc = sandbox.commands.run("cat", stdin=True, background=True)
    proc.send_stdin("abc\n")
    proc.close_stdin()
    result = proc.wait()
    assert result.stdout == "abc\n"
    assert result.stderr == ""
    assert result.exit_code == 0
```

`test_files.py`：

```python
def test_files_roundtrip(sandbox):
    info = sandbox.files.write("workspace/a.txt", "hello")
    assert info.name == "a.txt"
    assert info.path == "workspace/a.txt"
    assert sandbox.files.read("workspace/a.txt") == "hello"
    assert sandbox.files.exists("workspace/a.txt") is True
    sandbox.files.remove("workspace/a.txt")
    assert sandbox.files.exists("workspace/a.txt") is False
```

### 8.6 JS SDK 测试

```typescript
test('lifecycle', async () => {
  const sandbox = await Sandbox.create()
  expect(await sandbox.isRunning()).toBe(true)
  const info = await sandbox.getInfo()
  expect(info.sandboxId).toBe(sandbox.sandboxId)
  expect(info.state).toBe('running')
  expect(await sandbox.kill()).toBe(true)
})

test('command result', async () => {
  const sandbox = await Sandbox.create()
  const result = await sandbox.commands.run('echo hello')
  expect(result.stdout).toBe('hello\n')
  expect(result.stderr).toBe('')
  expect(result.exitCode).toBe(0)
  await sandbox.kill()
})
```

### 8.7 SDK 集成测试矩阵

| 场景 | Python sync | Python async | JS | 关键断言 |
|------|-------------|--------------|-----|----------|
| create | 是 | 是 | 是 | id/envdVersion 非空 |
| 按模板镜像创建 | 是 | 是 | 是 | `templateID` 映射到对应基础镜像并创建成功 |
| connect | 是 | 是 | 是 | 同一 sandboxID，命令可用 |
| list 分页 | 是 | 是 | 是 | 包含目标 sandboxID |
| get_info | 是 | 是 | 是 | state 为 running |
| kill | 是 | 是 | 是 | kill 后 is_running 为 false |
| set_timeout | 是 | 是 | 是 | endAt 更新 |
| 前台命令 | 是 | 是 | 是 | stdout/stderr/exitCode 精确 |
| 后台命令 | 是 | 是 | 是 | pid 非空，wait 精确 |
| 非零退出码 | 是 | 是 | 是 | 退出码精确 |
| 沙箱内安装用户级 CLI | 是 | 是 | 是 | 安装后命令可执行，产物位于沙箱目录内 |
| 配置镜像模板的 rootfs 执行 | 是 | 是 | 是 | 命令在模板配置的基础镜像 rootfs 内执行，不依赖宿主运行时 |
| 模板未配置镜像 | 是 | 是 | 是 | 纯 Sandlock，不创建外层容器 |
| 模板配置镜像 | 是 | 是 | 是 | 自动容器隔离 + 内层 Sandlock 策略生效 |
| stdin | 是 | 是 | 是 | 输入输出精确一致 |
| close_stdin | 是 | 是 | 是 | 子进程收到 EOF |
| commands.list/kill | 是 | 是 | 是 | 第二次 kill false |
| 命令超时 | 是 | 是 | 是 | timeout 异常，进程清理 |
| files.read | 是 | 是 | 是 | 内容精确一致 |
| files.write | 是 | 是 | 是 | EntryInfo 精确 |
| files.list/exists/get_info | 是 | 是 | 是 | 类型和路径精确 |
| files.remove/rename/make_dir | 是 | 是 | 是 | 状态精确 |
| files.watch_dir | 是 | 是 | 是 | create/write/remove 事件精确 |
| pty | 是 | 是 | 是 | 仅 spike 通过后验收；输出、resize、kill 正确 |
| 大文件 | 是 | 是 | 是 | hash 完全一致 |
| TTL 到期 | 是 | 是 | 是 | 沙箱消失 |

### 8.8 安全测试

| 测试 | 期望 |
|------|------|
| 写工作目录外 | 拒绝 |
| 读 `/etc/passwd` | 按策略拒绝 |
| 访问 `/sys`、`/proc/kcore` | 拒绝 |
| 默认网络 | 无法外联 |
| 内存超限 | Sandlock seccomp notif 限制 |
| 进程数超限 | 无法启动子进程 |
| 命令超时 | 进程组清理 |
| 路径穿越 | 不逃逸工作目录 |
| 恶意 sandboxID | 无越权路径/进程 |
| 安装命令写系统路径 | 被 Landlock 拒绝，只能写沙箱目录或显式 `fs_writable` 路径 |
| 模板镜像容器隔离 | 配置基础镜像的模板启用独立 namespace/cgroup，Sandlock 策略仍然生效；未配置模板保持纯 Sandlock |
| 总内存超限 | 创建返回 `503`，不启动 runtime |
| 总 CPU/磁盘/进程超限 | 创建返回 `503`，不启动 runtime |
| kill/TTL 后释放配额 | 同资源额度下可再次创建成功 |

### 8.9 性能测试

必须记录 profile：`py-spy dump` 或 `pprof`。

| 指标 | 目标 |
|------|------|
| 沙箱创建 P50 | <= 20ms |
| 命令首字节 P50 | <= 10ms |
| 单沙箱空闲内存 | <= 20MB |
| 100 并发 | 无死锁、无 fd 泄漏 |

### 8.10 CI

| Job | 系统 | 命令 |
|-----|------|------|
| 单元 + 契约 | macOS/Ubuntu | `pytest tests/unit tests/contract` |
| macOS 真实执行（可选） | macOS + Docker Desktop | `docker compose -f deploy/compose/docker-compose.test.yml run --rm test-runner pytest tests/sdk/python` |
| Python SDK | ubuntu-24.04 + Docker daemon | `E2B_BASE_IMAGE=python:3.11-slim pytest tests/sdk/python` |
| JS SDK | ubuntu-24.04 + Docker daemon | `E2B_BASE_IMAGE=python:3.11-slim pnpm test --run tests/sdk/js` |
| 安全 | ubuntu-24.04 + Docker daemon | `E2B_BASE_IMAGE=python:3.11-slim pytest tests/security` |
| 模板镜像隔离 | ubuntu-24.04 + Docker daemon | `E2B_BASE_IMAGE=python:3.11-slim pytest tests/security/test_template_isolation.py` |
| 性能 | 自托管 Linux + Docker daemon | `E2B_BASE_IMAGE=python:3.11-slim pytest tests/perf --perf` |

### 8.11 macOS 本机运行真实执行测试

macOS 本机不能直接执行 Sandlock。`deploy/compose/docker-compose.test.yml` 负责启动 Linux test runner：

```yaml
services:
  test-runner:
    build:
      context: ../..
      dockerfile: deploy/docker/Dockerfile.test-runner
    image: e2b-sandlock-test:latest
    working_dir: /workspace
    privileged: true
    environment:
      E2B_API_KEY: local-key
      E2B_API_URL: http://localhost:3000
      E2B_SANDBOX_URL: http://localhost:49983
      E2B_BASE_IMAGE: python:3.11-slim
      E2B_WORKSPACE_BASE: /workspace/tmp/sandboxes
    volumes:
      - .:/workspace
      - /var/run/docker.sock:/var/run/docker.sock
    command: pytest tests/sdk/python
```

`deploy/docker/Dockerfile.test-runner` 基线：

```dockerfile
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    docker.io \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir sandlock==0.8.6 e2b==2.46.1 pytest pytest-asyncio
```

执行命令：

```bash
docker compose -f deploy/compose/docker-compose.test.yml build
docker compose -f deploy/compose/docker-compose.test.yml run --rm test-runner
```

启动前必须检查 test runner 内 `sandlock.landlock_abi_version() >= 6`；不满足时改用 Linux CI runner。

## 9. 验收标准

1. Python sync/async 与 JS SDK 2.46.1 只设置三个环境变量即通过 L3 测试。
2. 支持范围内的官方 OpenAPI、proto、envd.yaml 通过契约测试。
3. 命令、stdin、文件、watch 行为与官方 SDK 完全一致；PTY 在适配 spike 通过后纳入验收。
4. 不支持范围返回明确错误。
5. 安全测试全部通过。
6. 性能测试有 profile，未放宽隔离。
7. 不再出现 `/commands/stream`、`/sandboxes/{id}/files/{path}` 等错误端点。
8. 总资源准入生效：任一总上限超限返回 `503`，kill/TTL 后释放配额并可恢复创建。
9. 沙箱内用户级 CLI 安装、持久化与跨命令复用通过集成测试；系统级包管理器不承诺。
10. 配置了基础镜像的模板在 Linux 6.12+ 的外层 test runner 容器/VM 内，通过模板镜像 rootfs 运行，并验证沙箱进程不访问宿主运行时；未配置镜像的模板走纯 Sandlock；macOS 本机不作为 Sandlock 验收环境。
11. 是否使用容器隔离由模板是否配置基础镜像决定；未配置则纯 Sandlock，配置则容器隔离 + 内层 Sandlock，并明确不等同 VM。
12. 模板基础镜像只通过 `templateID` 解析，官方 SDK 不支持直接传 `image`；未知模板返回 `400`。

## 10. 限制与路线图

### 10.1 当前限制

- 仅 Linux Kernel >= 6.12（完整 Landlock ABI v6；PyPI 包最低 6.7）。
- Sandlock 是进程级隔离，不等同 VM 内核隔离；强对抗场景不作为 VM 替代。
- 不支持 GPU。
- 不支持 pause/resume 内存快照。
- 不支持 Template/Volume/Secret/Snapshot/IAM/MCP；Network 支持 egress 白/黑名单、
  域名级 HTTP ACL（`rules`）与 `egressProxy`（SOCKS5 隧道），
  `maskRequestHost`/header 改写待 sandlock 注入能力发版。
- 单机部署。

### 10.2 路线图

| 版本 | 内容 |
|------|------|
| v2.1 | Envd 多实例路由 |
| v2.2 | Template 本地构建 |
| v2.3 | Volume |
| v2.4 | Snapshot |
| v3.0 | Kubernetes |

### 10.3 与 E2B VM 的能力差距

Sandlock 是进程级沙箱，不是 MicroVM。对常见 AI Agent 代码执行（Python/JS、Shell、文件、用户级包安装、网络白名单、资源限制）可以覆盖，但不能宣称完整替代 E2B VM。

| 能力 | E2B VM | Sandlock 实现 | 差距/风险 |
|------|--------|----------------|-----------|
| 内核隔离 | 独立 guest kernel | 共享宿主 kernel | 宿主内核漏洞可能影响整机；强对抗场景风险高 |
| Root/系统安装 | VM 内可 root 安装 | 无真实 Root，仅用户级安装或假 Root | `apt-get`/`dnf`/systemd 不保证 |
| 文件系统 | 完整磁盘、overlay、mount | Landlock + COW/chroot/fs_mount | 不能任意 mount，完整 rootfs 语义有限 |
| 网络 | 完整网络栈、TUN/TAP、raw socket | `net_allow`/`net_deny`/bind/HTTP ACL | 无完整接口、VPN、raw socket |
| Pause/Resume/Snapshot | VM 内存/磁盘快照 | 进程级 pause/checkpoint | 不等同 E2B VM 快照语义 |
| 磁盘配额 | VM 磁盘硬限制 | `max_disk` 仅 COW | 非 COW 写入无硬限制 |
| 资源限制 | cgroup/VM 强隔离 | seccomp notif + SIGSTOP | 内存/CPU 为近似限制 |
| GPU | VM 透传 | `gpu_devices` 暴露设备节点 | 可运行部分 GPU 程序，不等同完整透传 |
| Desktop/GUI | 完整桌面模板 | 无内置桌面 | 不支持浏览器/桌面场景 |
| 公网端口 | 自动 HTTPS 暴露 | `net_allow_bind` + 自建代理 | 需要额外实现反向代理 |
| 内核版本 | 独立 guest kernel | 依赖宿主 Linux 6.12+ | 部署面受限 |

安全结论：Sandlock 适合内网可信度较高、强调低延迟和低成本的代码执行场景；不适合公开多租户、需要 VM 级对抗隔离的强安全场景。此时应继续使用 E2B 云沙箱或 MicroVM。

## 11. 附录

### 11.1 契约来源

- 控制面：官方 `spec/openapi.yml`。
- envd RPC：`spec/envd/process/process.proto`、`spec/envd/filesystem/filesystem.proto`。
- envd HTTP：`spec/envd/envd.yaml`。
- 兼容基线：`e2b==2.46.1`、`e2b@2.46.1`。

### 11.2 关键约束

- 两个服务分别启动、分别测试。
- 时间字段使用 RFC3339/ISO 8601 UTC。
- JSON 字段名遵循官方 camelCase。
- Connect 流式响应必须使用 `application/connect+json`。
- `E2b-Sandbox-Id`、`E2b-Sandbox-Port` 不得被代理丢弃。
- 实现必须 TDD：先让契约测试失败，再补齐生产代码。
