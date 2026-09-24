# Sandlock E2B 容器化 Worker 自动扩缩容方案

> 状态：设计稿（v4，请求队列改为自适应预热 + 延迟 ID 幂等创建）
> 范围：worker 节点容器化部署、按请求容量自动扩缩容；同时支持本地 Docker 扩容与 Kubernetes 扩容。
> 强制前提：运行时不再依赖 Docker daemon socket；镜像统一存放于
> `registry.cn-shanghai.aliyuncs.com/byteplan/`。

## 1. 目标

1. Worker（envd_service）以容器形式部署，数量随请求容量自动伸缩。
2. 同一套决策逻辑同时驱动两种执行后端：
   - 本地：Docker 宿主上的容器池（Pool Manager）；
   - 云上：Kubernetes Deployment 副本数（或后续演进为 HPA + custom metrics）。
3. 运行时（控制面 / worker / gateway / autoscaler）完全去掉 Docker socket 依赖，
   镜像 rootfs 提取改为直接走 OCI Registry v2 API。
4. 缩容不丢沙箱：只回收"无活跃沙箱"的节点，回收前先进入 draining 状态。
5. 创建请求按镜像预热状态自适应：镜像已缓存时服务端直接创建（官方 SDK
   无感）；需要长时间预热时要求客户端携带幂等 sandbox-id，预热成功前不
   创建记录，重试同 ID 立即返回，杜绝孤儿沙箱与重复创建。

## 2. 现状与结论

控制面已具备动态扩缩容的调度基础，无需重写：

- `NodeRegistry`：worker 启动时向 `/internal/nodes/register` 注册容量
  （memory / cpu / disk / processes），之后每 5s 心跳；`select_and_reserve`
  在锁内做容量检查与原子预留（`control_plane/registry/nodes.py`）。
- 调度器：过滤 healthy 节点 -> 镜像亲和 -> 剩余容量打分；无节点可容纳时
  先驱逐空闲沙箱（E9.3）并等待容量释放（E9.4 创建排队，默认 30s），
  超时才返回 503 "No resources available"（`control_plane/api/sandboxes.py`）。
- Redis 配额存储已支持控制面多副本（`control_plane/registry/redis_backend.py`）。
- gateway 按 `sandbox_id -> node address` 路由，worker 只需对 gateway
  HTTP 可达；共享 workspace 卷已支持跨节点迁移。

结论：**扩缩容的本质是"根据集群剩余容量调整 worker 副本数"**，需要新增
的是一个决策者（Autoscaler）+ 两个执行后端，外加四块前置改造：

1. 去掉 Docker socket（本方案第 4 节）；
2. 控制面增加自适应预热 + 延迟 ID 幂等创建（本方案第 5 节）；
3. 控制面增加 draining 状态与聚合指标接口（第 9 节）；
4. worker 增加优雅停机与镜像预热（第 10 节）。

## 3. 总体架构

```
请求 -> gateway -> control plane（调度 + 配额预留 + 预热预判）
                     │  /internal/nodes（每节点 reserved/total）
                     ▼
         Autoscaler 决策循环（5~10s）
                     │  desired = f(利用率, 503 率, 冷却)
                     ├── backend: local   （Docker Pool Manager）
                     └── backend: k8s     （Deployment replicas）
```

决策指标（"请求容量"语义）：

- 主指标：集群剩余容量。聚合每节点 `reserved/total`（四维），换算为
  "还能容纳多少个标准沙箱"：`Σ floor((total_i - reserved_i) / 单沙箱需求)`，
  通常内存是瓶颈维度。
- 辅助指标：最近 N 分钟 503 "No resources available" 错误率（兜底快扩）。
- 可选增强：控制面记录创建请求 RPS，指数平滑后做预测扩容。

desired 副本数：

```
max(min_replicas,
    ceil((活跃沙箱数 + 预测增量) / 每 worker 可容纳数) + warmup_buffer)
```

预计并发沙箱数 = 活跃沙箱（+ 可选 RPS × 扩容延迟的预测增量）。
每 worker 可容纳数 = 各维度 `E2B_NODE_*_MB / 默认沙箱需求` 的最小值
（`can_fit` 的逆运算）。进程维度按**整箱语义**记账（M4 D1–D3 起每沙箱
一个 sandlock 实例，命令与 MCP 网关共享箱内预算）：单沙箱 `max_processes`
默认已从 64 上调到 256（M4 D6），节点 `total_processes` 默认不变（内嵌
本地节点取 `E2B_MAX_TOTAL_PROCESSES`，默认 2048），因此该维度每节点最多
容纳 `2048 / 256 = 8` 个标准沙箱（此前 `2048 / 64 = 32`）——默认上调后
进程维度不再是富余维度，四维容量换算必须显式计入。
内存维度随 FUP #3 同步上调（2026-09-06）：单沙箱默认内存从 512 MiB 提到
1 GiB（`E2B_DEFAULT_MEMORY_MB`），默认 8192 MiB 节点因此容纳
`8192 / 1024 = 8` 个标准沙箱（此前 `8192 / 512 = 16`），与进程维度同为
容量主约束；换算必须同时显式计入内存与进程维度。

M4 收口（2026-09-06）：执行边界 = 产品边界——每沙箱一只 exec-only
`SandboxInstance`（命令与 MCP 网关共享箱内预算），"每命令一个实例可超卖"的形态已随
`third_party/sandlock/docs/e2b-integration.md` §3.8 关闭。未在容量公式内放宽的
fork-blocked 边界只剩 fork F11（多线程进程存在后 argv-safety exec 冻结 EPERM，
见 `docs/task-backlog.md`「M4 收口后的 open follow-ups」）。网关 ledger headroom
已关闭在 E2B 侧（FUP #3）：默认箱从 512 MiB 提到 1 GiB 后，网关 allocator
reservations（~250–330M）与 450M MCP server 目标可共存，fork 逻辑未改动。

冷却策略：扩容冷却 60s；缩容冷却 10min，且仅当集群聚合利用率低于
`E2B_AS_SCALE_DOWN_UTIL`（默认 0.40）、候选节点连续空闲且 0 活跃沙箱时
才生效。

## 4. 去掉 Docker Socket（强制前提）

### 4.1 现状：两处依赖

| 位置 | 用途 | 现状 |
|---|---|---|
| `envd_service/runtime/image_resolver.py` | 把 base image 提取为 chroot rootfs | `docker create` + `docker export`，需要 daemon socket |
| `control_plane/api/templates.py` | 模板构建 | `docker build` / `tag` / `push`，需要 daemon socket |

运行时镜像（`deploy/docker/Dockerfile.envd` / `deploy/docker/Dockerfile.control-plane`）目前都安装了
`docker.io docker-cli`，compose 文件都挂载了 `/var/run/docker.sock`。

### 4.2 Worker：registry 直拉 rootfs 提取器（核心改造）

`resolve_image_rootfs` 改为**不依赖任何 daemon**，直接按 OCI Distribution Spec
从镜像仓库拉取并组装 rootfs：

1. **解析镜像引用**：`registry.cn-shanghai.aliyuncs.com/byteplan/xxx:tag`
   -> host / repo / tag；支持 `@sha256:...` digest 引用；无 host 时默认
   Docker Hub（仅本地开发兼容，生产必须全限定）。
2. **鉴权**：先 `GET /v2/`，按 `WWW-Authenticate` 响应走 Basic（私有仓库）
   或 Bearer token 交换（`realm?service=...&scope=repository:<repo>:pull`）。
   Aliyun ACR 公共仓库可匿名拉取；私有仓库使用
   `E2B_IMAGE_REGISTRY_USERNAME` / `E2B_IMAGE_REGISTRY_PASSWORD`。
3. **取 manifest**：`GET /v2/<repo>/manifests/<tag>`，Accept 同时声明
   Docker manifest list / OCI index / Docker v2 / OCI manifest；返回 index 时
   按 worker 自身架构（amd64 / arm64）选平台子 manifest。
4. **下载层**：按 manifest 的 `layers[]` 顺序 `GET /v2/<repo>/blobs/<digest>`，
   必须跟随重定向（Aliyun 会把 blob 302 到 OSS/CDN）。
5. **解包组装**（顺序关键，白名单语义与 OCI 一致）：
   - gzip 层用 tarfile 解压到缓存 rootfs；
   - `.wh.<name>` 白out 条目 -> 删除目标路径；
   - `.wh..wh..opq` 不透明目录 -> 清空该目录既有内容后再解包；
   - 沿用 `envd_service/agent.py` 现有的 tar 成员过滤（防路径逃逸、
     跳过绝对符号链接）。
6. **缓存**：cache key = `image-cache-name-<manifest digest>`（沿用现有
   `_image_cache_name` + digest 后缀），`<rootfs>/.complete` 标记完成；
   tag 更新时 digest 变化，旧缓存自然失效。
7. **并发锁**：进程内按 image 加锁（或文件锁），避免并发创建沙箱时重复
   解包；失败清理半成品目录。
8. **完整性校验**：解包后 `bin/` 或 `usr/bin/` 必须存在，否则视为空 rootfs。

产物契约与现状完全一致：rootfs 目录 + `.complete` 标记，`SandlockExecutor`
仅把该目录当作 chroot 使用（不读镜像 config 的 ENV / Entrypoint），
因此**无需解析 config blob**，改造不触碰执行器。

依赖变化：`deploy/docker/Dockerfile.envd` 移除 `docker.io docker-cli`；compose / k8s
manifest 移除 `/var/run/docker.sock` 挂载。

### 4.3 控制面：模板构建去 daemon 化

模板构建（`POST /templates/{id}/build`）现有实现依赖本地 docker build。
去 socket 后的两条路径：

- **推荐（先落地）：构建外置到 CI/CD**。模板镜像在发布流水线中
  `docker buildx build --push` 到 `registry.cn-shanghai.aliyuncs.com/byteplan/`，
  控制面只登记镜像引用（`template.image` 指向仓库全限定名），运行时不再
  需要构建能力。
- **可选（后续）：独立 Builder 服务**。用 BuildKit（daemonless，buildctl +
  buildkitd，不走 docker socket）或 K8s 内 kaniko 提供构建，控制面把
  Dockerfile 交给 Builder，产物 push 回仓库。改动大，作为二期。

一期控制面 `deploy/docker/Dockerfile.control-plane` 移除 `docker.io docker-cli`；模板构建
接口在 CI 路径未接通前保持"构建失败并提示改用 CI"的降级行为（或由配置
开关 `E2B_TEMPLATE_BUILD_ENABLED=false` 直接禁用）。

### 4.4 镜像与镜像仓库约定

所有运行时镜像统一推送到 Aliyun ACR（上海），镜像名：

| 组件 | 镜像 |
|---|---|
| 控制面 | `registry.cn-shanghai.aliyuncs.com/byteplan/e2b-sandlock:control-plane` |
| Worker | `registry.cn-shanghai.aliyuncs.com/byteplan/e2b-sandlock:worker` |
| Gateway | 复用 worker 镜像，`command` 覆盖为 `gateway_main` |
| Autoscaler | `registry.cn-shanghai.aliyuncs.com/byteplan/e2b-autoscaler:latest` |
| 模板镜像 | `registry.cn-shanghai.aliyuncs.com/byteplan/templates/<name>:<tag>` |

约定：

- 生产/测试环境模板镜像与 base image 一律写全限定名（含 host），杜绝
  隐式 Docker Hub 解析；
- 私有仓库凭据只通过 `E2B_IMAGE_REGISTRY_USERNAME` /
  `E2B_IMAGE_REGISTRY_PASSWORD` 注入（worker 提取与 CI push 共用）；
- `deploy/scripts/build-images.sh` 默认 `TAG` 改为上述 registry 前缀，`PUSH=1`
  才推送。

## 5. 自适应预热 + 延迟 ID 幂等创建（孤儿沙箱的解法）

### 5.1 问题与思路

- 根因：客户端超时（SDK 默认请求超时 60s）后服务端可能仍在预热 / 创建，
  沙箱实际存在但客户端拿不到 sandbox_id -> 孤儿。
- 思路：**预热成功前不创建任何记录**（无孤儿可言）；需要长时间预热时
  强制客户端携带幂等 sandbox-id，重试同 ID 立即命中已有记录（不重复创建）。
- 自适应：镜像已缓存 -> 快路径，服务端直接创建（官方 SDK 无感）；镜像
  未缓存 -> 慢路径，要求 `X-Sandbox-Id`。

### 5.2 预热预判

- 预热成本 = 镜像 rootfs 提取（拉取 + 解包），digest 级缓存
  （`<rootfs>/.complete` 标记），幂等且可预判。
- 新增 worker 端点 `GET /agent/images/{image}/warm` -> `{cached, digest}`，
  只查缓存标记（毫秒级），供控制面决定快 / 慢路径。
- 控制面流程：`select_and_reserve`（选节点 + 预留配额）后调用该端点预判；
  可选：worker 注册 / 心跳时上报缓存镜像列表，控制面本地预判（省一次 RPC，
  但 digest 可能过期，准确性低于端点）。

### 5.3 快路径（镜像已缓存）

- 服务端生成 `sandbox_id` -> `registry.create` -> provision -> 201 直接返回；
- 官方 SDK 默认行为不变（不带任何 header 也能创建）；
- 边界：探针与 provision 之间镜像可能失效（tag 更新），快路径 provision
  因镜像问题失败时兜底走慢路径或直接报错，不留半成品。

### 5.4 慢路径（镜像未缓存，强制幂等）

- 客户端通过 `api_headers={"X-Sandbox-Id": "sbx_..."}` 携带自生成的 ID
  （官方 e2b SDK 2.46 的 `api_headers` 会附加到 create 请求头，无需 fork）；
- 带 ID：
  1. Redis `SET NX` 抢占 pending（`key -> {node, status}`，带 TTL），并发同
     ID 请求输家等待，避免重复预热；
  2. 节点预热 `resolve_image_rootfs`（digest 缓存幂等）；
  3. 预热成功才用客户端 ID 建记录 -> provision -> 201；
  4. 预热 / provision 失败：释放配额、清 pending、删除已建记录，不留孤儿。
- 不带 ID：**快速失败**——返回 428 + 错误码 `warm_required` + 结构化提示
  （"重试时带上 X-Sandbox-Id"），释放配额，不挂起不预热。
- 重试语义：
  - 同 ID 已有记录 -> 立即 201；
  - pending 中 -> 等待落定（剩余请求超时内）；
  - 全新 -> 重走慢路径（缓存命中后即快路径）。

### 5.5 幂等保证边界

- 只对携带 `X-Sandbox-Id` 的客户端生效；官方 SDK 默认路径（不带 header）
  在快路径下无感，在冷图上得到可读的 `SandboxException`（428
  `warm_required`），由调用方决定是否带 ID 重试；
- ID 校验：`sbx_` 前缀 + 合法字符，防注入 / 碰撞；
- TTL sweeper 仍为最终兜底：任何原因产生的孤儿都会被定时回收（默认 TTL
  300s），不是永久泄漏。

### 5.6 SDK 行为影响与约束

基于官方 e2b SDK 2.46.0 源码核实（本项目测试锁定的版本；JS SDK 2.46.1
为同一套 REST 语义）：

- **协议无感**：仍然是同一个 `POST /sandboxes`，快路径 201 直接返回、慢
  路径带 ID 重试后同样 201；SDK 调用方式、参数、返回结构均不变。
- **幂等键载体**：官方 SDK 2.46 的 `api_headers` 会附加到 create 请求头
  （`connection_config.py` 已核实），无需 fork SDK；
- **SDK 无客户端重试**：503 / 428 均映射为 `SandboxException`（429 ->
  `RateLimitException`，401 -> `AuthenticationException`）；重试由调用方
  决定（建议带同 ID + 退避）。
- **请求超时是硬约束**：SDK 默认请求超时 60s（`REQUEST_TIMEOUT`，可用
  `request_timeout` 覆盖）。慢路径预热 + 创建必须远小于 60s，否则客户端
  超时；缓解：worker 启动预热、warm pool、预热缓存（第二次即快路径）。
- **孤儿防护**：预热成功前无记录；带 ID 的请求即使客户端超时，重试同 ID
  也能命中；TTL sweeper 兜底，见 5.5。

## 6. Autoscaler 组件设计

### 6.1 职责

- 轮询控制面 `GET /internal/fleet/metrics`（新接口，见 9.3）；
- 按策略计算 desired 副本数（阈值、预测、冷却、min/max）；
- 调后端收敛实际副本数；
- 缩容前先 drain（见第 9 节），确认节点无活跃沙箱后再回收。

### 6.2 策略参数（环境变量配置）

| 参数 | 默认 | 说明 |
|---|---|---|
| `E2B_AS_MIN_REPLICAS` | 1 | 常驻 warm pool 下限 |
| `E2B_AS_MAX_REPLICAS` | 16 | 上限 |
| `E2B_AS_UTIL_THRESHOLD` | 0.70 | 主维度利用率达到即扩 |
| `E2B_AS_SCALE_UP_COOLDOWN_S` | 60 | 扩容冷却 |
| `E2B_AS_SCALE_DOWN_COOLDOWN_S` | 600 | 缩容冷却 |
| `E2B_AS_SCALE_DOWN_UTIL` | 0.40 | 集群聚合利用率低于该值才允许缩容 |
| `E2B_AS_NODE_SCALE_DOWN_UTIL` | 0 | 候选节点利用率低于该值（0=完全空闲） |
| `E2B_AS_IDLE_BEFORE_DRAIN_S` | 300 | 节点空闲多久才允许 drain |
| `E2B_AS_WARMUP_BUFFER` | 1 | 额外缓冲副本数（覆盖冷启动） |
| `E2B_AS_POLL_S` | 5 | 决策周期 |

### 6.3 后端接口

```python
class ScaleBackend(Protocol):
    def current(self) -> int: ...
    def desired(self, n: int) -> None: ...
    def ready(self) -> int: ...          # 已注册且 healthy 的副本数
    def drain(self, node_id: str) -> None: ...
```

- `local.py`：Docker Pool Manager（第 7 节）；
- `k8s.py`：K8s API 改 Deployment replicas（第 8 节）。

## 7. 本地容器扩容（backend: local）

### 7.1 为什么不用 `docker compose --scale`

compose scale 的副本共享同一份 env，`E2B_NODE_ADDRESS` 无法按副本注入，
多个副本会注册成同一地址。因此本地采用 **Pool Manager**：

- 轻量 daemon（Python，复用本项目风格），维护"期望列表 <-> 实际容器列表"
  的收敛循环，状态落盘到项目 `tmp/`；
- 扩容：`docker run -d --name e2b-worker-<n> --network <共享网络> ...`，
  注入唯一 `E2B_NODE_ID` / `E2B_NODE_ADDRESS=http://e2b-worker-<n>:49983`
  （Docker 网络内容器名可解析，gateway 同网络可达）；
- 运行参数与现状一致：`seccomp=unconfined`、
  `--sysctl net.ipv4.ip_unprivileged_port_start=0`（容器 spec 声明，A6 之后
  不再需要 `--cap-add SYS_ADMIN`：共享卷 bind 已删、配额走 quota-agent）、
  共享 workspace 卷挂载；
- 缩容：先调控制面 drain（第 9 节），活跃沙箱归零后 `docker rm -f`；
- 启动时对已有容器做对账（孤儿回收 / 缺失补齐），幂等可重启。

备选增强（可选小改）：worker 的 `E2B_NODE_ADDRESS` 缺省时用
`socket.gethostname()` 拼 `http://<hostname>:49983`，这样 compose
`--scale` 也能直接工作（容器名即 DNS 名），Pool Manager 与原生 scale
可共存。

## 8. Kubernetes 部署扩容（backend: k8s）

### 8.1 部署形态

- control-plane：Deployment 多副本 + Service（Redis 已支持共享状态）；
- gateway：Deployment + Service；
- worker：Deployment + headless Service（`publishNotReadyAddresses: true`），
  pod 通过 downward API 注入：

```yaml
env:
  - name: E2B_NODE_ID
    valueFrom: { fieldRef: { fieldPath: metadata.name } }
  - name: E2B_NODE_ADDRESS
    value: http://$(POD_NAME).worker-headless.<ns>.svc.cluster.local:49983
  - name: POD_NAME
    valueFrom: { fieldRef: { fieldPath: metadata.name } }
```

- autoscaler：Deployment，`serviceAccount` 授权 `apps/deployments/scale` +
  `statefulsets/scale`；
- 共享 workspace：RWX PVC（NFS / CephFS）挂所有 worker，覆盖
  `E2B_WORKSPACE_BASE` 与快照/卷路径；现有 `shared_volume_root` 亲和判定
  逻辑直接复用。

### 8.2 worker pod 关键配置

- 资源：`resources.limits` 必须显式设置，且 `E2B_NODE_MEMORY_MB` /
  `E2B_NODE_CPU_PERCENT` / `E2B_NODE_DISK_MB` / `E2B_NODE_PROCESSES`
  必须等于 limits（容器内探测会看到宿主机全部资源，不显式覆盖会严重超卖）；
- 权限：`securityContext.capabilities.add: [NET_BIND_SERVICE]`、
  `securityContext.seccompProfile.type: Unconfined`（K8s 1.19+）。
  **A6 之后不再需要 `SYS_ADMIN`**（共享卷 bind 已删、配额由 quota-agent 提供，
  见 `docs/production-deployment-requirements.md` §2.4.3）；低端口 `:53` 在 netns
  形态下不需要任何窗口（见下条），cap 对非 root pod 无效；
- 非 root（E5.1）：worker 镜像以 uid 65534 运行，Pod 同步声明
  `securityContext.runAsNonRoot: true`、`runAsUser: 65534`、
  `runAsGroup: 65534`（镜像已预建 `/var/lib/e2b-sandboxes` 且属主 65534；
  既有 RWX PVC 需一次性 chown 到 65534，或由 initContainer 完成）；
- 低端口：**netns 形态下不需要**。`deploy/k8s/worker.yaml` 自 2026-09-17（N5）起与
  compose stack 同形态（`E2B_ENABLE_NET_ISOLATION` + `E2B_FD_INJECT_CONNECT` 成对），
  pod 级 `securityContext.sysctls` 随之删除：wildcard allowOut 的 DNS 网关绑 `:53` 现在
  发生在**沙箱自己的 netns** 里，root-in-userns 自带 `CAP_NET_BIND_SERVICE`（fork
  `context.rs`）。只有**共享 netns 形态**才需要低端口窗口，且在那里 worker 镜像
  `USER 65534` ⇒ 非 root 的 cap 没有 effective 语义（Kubernetes 无 ambient
  capabilities），`NET_BIND_SERVICE` **不够**，内核默认 1024 下 bind `:53` = EACCES。
  实测来自 **Docker 引擎**（`tmp/a6fix1-cap-probe.log`：uid 65534 + cap ⇒ `CapEff=0`；
  同一 uid 声明 sysctl=0 ⇒ OK；root + cap ⇒ OK）；k8s 侧的旧依据是该 sysctl 自 1.22 起
  属 safe sysctl（可声明、无需 kubelet 放行，`hostNetwork: true` 下 `net.*` 会被拒）；
  cap 只对 root override（`runAsUser: 0`）有意义；
- 无 docker socket：不挂 `/var/run/docker.sock`，rootfs 提取走第 4.2 节
  的 registry 直拉。

### 8.3 扩容路径与演进

- 一期：Autoscaler 直接调 K8s API 改 Deployment replicas。指标数据本来就
  在控制面，自研组件两端复用，比 KEDA + prometheus-adapter 基础设施简单；
- 二期（可选）：把 `/internal/fleet/metrics` 导出为 custom metric，
  接 prometheus-adapter + 原生 HPA；
- 节点级：集群机器不足时配合 cluster-autoscaler（pod 无法调度 -> 加节点）。

## 9. 缩容安全（两环境共用）

### 9.1 draining 语义

- `NodeRecord` 增加 `draining: bool`；
- 调度器（`scheduler.py` / `select_and_reserve`）过滤 draining 节点，
  新沙箱不再调度上去；
- 存量沙箱继续服务，直到全部销毁 / TTL 过期 / 迁移。

### 9.2 控制面新增接口

- `POST /internal/nodes/{node_id}/drain`：置 draining，返回该节点活跃沙箱数；
- `GET /internal/fleet/metrics`：聚合返回每节点利用率、活跃沙箱数、
  集群剩余可容纳沙箱数、最近 5 分钟 503 无资源错误数。

### 9.3 回收条件（autoscaler 判定）

1. 集群级：聚合主维度利用率（Σreserved / Σtotal）<
   `E2B_AS_SCALE_DOWN_UTIL`（默认 0.40），整体需求低时才允许缩容——避免
   高负载下误缩造成扩容抖动（缩下去马上又扩回来）；
2. 节点级：候选节点利用率 < `E2B_AS_NODE_SCALE_DOWN_UTIL`（默认 0，完全
   空闲），且连续 `E2B_AS_IDLE_BEFORE_DRAIN_S` 秒无新沙箱；
3. 调 drain 后活跃沙箱数 = 0；
4. 全局副本数仍 >= min_replicas；
5. 缩容冷却已过。

K8s 侧再叠加 `terminationGracePeriodSeconds`（如 120s）+ PDB
（`minAvailable: 1`）；SIGTERM 后 worker 停止心跳、拒绝新沙箱，存量沙箱
继续服务到清空或 grace 超时（本地 Pool Manager 只做
`docker rm -f`，不主动 SIGKILL 正在服务的容器）。

## 10. 冷启动与预热

扩容延迟 = 容器启动 + sandlock 初始化 + 模板镜像拉取/解包，远大于决策周期，
因此：

1. 阈值保守：主维度利用率 60~70% 即扩，留出新副本 Ready 时间；
2. warm pool：`min_replicas` 常驻，镜像已缓存；
3. worker 启动时预热：agent 启动阶段按 `E2B_BASE_IMAGE`（或配置的模板
   列表）调用一次 `resolve_image_rootfs`，把常用 rootfs 提前解包；
4. 模板镜像 CI 预推仓库，worker 首拉由 registry CDN 加速（Aliyun ACR）；
5. provisioning 预算约束：慢路径（预热 + 创建）全程必须远小于 SDK 默认
   请求超时 60s，不满足时要求调用方显式传 `request_timeout`，或降低预热
   成本（预热列表收敛、预推镜像）；
6. TTL sweeper 为最终孤儿兜底（默认 TTL 300s），与第 5.5 节一致。

## 11. 落地路线图与改动清单

### Phase 0：仓库与镜像基建

- `deploy/scripts/build-images.sh` 默认 TAG 指向
  `registry.cn-shanghai.aliyuncs.com/byteplan/e2b-sandlock`；
- `deploy/compose/.env.example` 补充 ACR 仓库地址与凭据占位；CI 增加 buildx push 任务。

### Phase 1：去 Docker socket

- `envd_service/runtime/image_resolver.py`：重写为 registry v2 直拉
  （第 4.2 节），保留 `resolve_image_rootfs` 签名；
- `deploy/docker/Dockerfile.envd` / `deploy/docker/Dockerfile.control-plane`：移除 docker CLI；
- `deploy/compose/docker-compose*.yml`：移除 socket 挂载；worker / control-plane 环境变量
  指向 ACR；
- 单元测试：本地起一个最小 OCI 假仓库（Python http.server 实现 manifest/
  blob），覆盖白 out / 不透明目录 / 鉴权 / 重定向 / 并发锁；
- `deploy/scripts/smoke-prod-worker.sh`：去掉 docker socket 前置说明，改为验证
  registry 直拉路径。

### Phase 2：控制面扩缩容能力

- `control_plane/registry/nodes.py`：draining 字段 + 过滤；
- `control_plane/scheduler.py`：排除 draining；
- `control_plane/api/internal.py`：drain 接口 + fleet metrics 聚合接口；
- `envd_service/http/`：新增 `GET /agent/images/{image}/warm` 缓存预判端点；
- `control_plane/registry/manager.py`：`registry.create` 支持显式
  `sandbox_id`；
- `control_plane/api/sandboxes.py`：创建流程自适应（快 / 慢路径 +
  `X-Sandbox-Id` 幂等 + Redis pending `SET NX` 去重 + 428 `warm_required`）；
- 单测：draining 不调度、metrics 聚合正确性、幂等创建（同 ID 重试不重复、
  冷图无 ID 快速失败、并发同 ID 去重）。

### Phase 3：Autoscaler + 本地后端

- 新增 `autoscaler/`：`policy.py` / `loop.py` / `backends/local.py`；
- 新增 `deploy/compose/docker-compose.autoscale.yml`（control-plane + gateway + autoscaler +
  worker pool 参数）；
- 本机端到端验证：并发创建压到阈值 -> 自动起 worker -> 请求成功；空闲后
  自动回收。

### Phase 4：K8s 后端与 manifest

- 新增 `deploy/k8s/`：control-plane / gateway / worker(headless) /
  autoscaler / RWX PVC / RBAC / PDB；
- `autoscaler/backends/k8s.py` 接 K8s API；
- 先静态 replicas 验证 worker 注册与路由，再开自动。

### Phase 5：压测标定

- 用现有 `tests/perf` 标定单 worker 吞吐与冷启动耗时，回填阈值参数；
- 压测记录 profile（遵守全局性能规范）。

## 12. 验收标准

1. 运行时任何组件都不挂 docker socket，镜像全量来自
   `registry.cn-shanghai.aliyuncs.com/byteplan/`；
2. 本地模式：构造请求压力，worker 容器自动从 min 扩到 N，请求无
   503（阈值内）；压力消失且集群利用率低于缩容阈值后才缩回 min；
3. 快路径：镜像已缓存时官方 SDK 无感创建（201 直接返回，不带 header）；
4. 慢路径：冷图无 ID 快速失败（428 `warm_required`）且无残留；带
   `X-Sandbox-Id` 重试同 ID 不重复创建，客户端超时 / 断开后无孤儿；
5. K8s 模式：同一套决策逻辑驱动 Deployment 副本数，worker pod 自动注册
   到控制面并被 gateway 正常路由；
6. 缩容全程活跃沙箱存活率 100%（drain 后新请求不落该节点）；
7. 全部既有测试通过（新 resolver 用假仓库单测覆盖，不依赖宿主机 docker）。

## 13. 可选增强（暂不进入一期）

- 服务端有界请求队列已落地（E9.4：`E2B_CREATE_QUEUE_TIMEOUT_S` /
  `E2B_CREATE_QUEUE_MAX`，驱逐后仍无容量时等待配额释放，超时 503）；
  延迟 ID + 客户端幂等重试仍负责客户端断连 / 超时场景；
- 模板构建 Builder 服务（BuildKit / kaniko），恢复"控制面触发构建"能力；
- HPA + custom metrics 原生扩缩容；
- cluster-autoscaler 节点级伸缩。
