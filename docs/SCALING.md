# Sandlock E2B 容器化 Worker 自动扩缩容方案

> 状态：设计稿（v3，补充 SDK 行为约束）
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
5. 容量不足时创建请求进入有界等待队列（而非立即 503），autoscaler 按队列
   深度快速扩容，超时或队满才失败。

## 2. 现状与结论

控制面已具备动态扩缩容的调度基础，无需重写：

- `NodeRegistry`：worker 启动时向 `/internal/nodes/register` 注册容量
  （memory / cpu / disk / processes），之后每 5s 心跳；`select_and_reserve`
  在锁内做容量检查与原子预留（`control_plane/registry/nodes.py`）。
- 调度器：过滤 healthy 节点 -> 镜像亲和 -> 剩余容量打分；无节点可容纳时
  返回 503 "No resources available"（`control_plane/api/sandboxes.py`）。
- Redis 配额存储已支持控制面多副本（`control_plane/registry/redis_backend.py`）。
- gateway 按 `sandbox_id -> node address` 路由，worker 只需对 gateway
  HTTP 可达；共享 workspace 卷已支持跨节点迁移。

结论：**扩缩容的本质是"根据集群剩余容量与等待队列调整 worker 副本数"**，
需要新增的是一个决策者（Autoscaler）+ 两个执行后端，外加四块前置改造：

1. 去掉 Docker socket（本方案第 4 节）；
2. 控制面增加请求队列（本方案第 5 节）；
3. 控制面增加 draining 状态与聚合指标接口（第 9 节）；
4. worker 增加优雅停机与镜像预热（第 10 节）。

## 3. 总体架构

```
请求 -> gateway -> control plane（调度 + 配额预留 + 容量不足入队）
                     │  /internal/nodes（每节点 reserved/total）
                     ▼
         Autoscaler 决策循环（5~10s）
                     │  desired = f(利用率, 队列深度, 503 率, 冷却)
                     ├── backend: local   （Docker Pool Manager）
                     └── backend: k8s     （Deployment replicas）
```

决策指标（"请求容量"语义）：

- 主指标：集群剩余容量 + 队列深度。剩余容量聚合每节点 `reserved/total`
  （四维），换算为"还能容纳多少个标准沙箱"：
  `Σ floor((total_i - reserved_i) / 单沙箱需求)`，通常内存是瓶颈维度；
  队列深度（等待中的创建请求数）直接反映瞬时需求缺口，是扩容的最强信号。
- 辅助指标：最近 N 分钟队满 / 超时导致的 503 错误率（兜底快扩）。
- 可选增强：控制面记录创建请求 RPS，指数平滑后做预测扩容。

desired 副本数：

```
max(min_replicas,
    ceil((活跃沙箱数 + 队列深度 + 预测增量) / 每 worker 可容纳数)
    + warmup_buffer)
```

预计并发沙箱数 = 活跃沙箱 + 队列深度（+ 可选 RPS × 扩容延迟的预测增量）。
每 worker 可容纳数 = 各维度 `E2B_NODE_*_MB / 默认沙箱需求` 的最小值
（`can_fit` 的逆运算）。

冷却策略：扩容冷却 60s；缩容冷却 10min，且只对"连续空闲 + 0 活跃沙箱"
的节点生效。

## 4. 去掉 Docker Socket（强制前提）

### 4.1 现状：两处依赖

| 位置 | 用途 | 现状 |
|---|---|---|
| `envd_service/runtime/image_resolver.py` | 把 base image 提取为 chroot rootfs | `docker create` + `docker export`，需要 daemon socket |
| `control_plane/api/templates.py` | 模板构建 | `docker build` / `tag` / `push`，需要 daemon socket |

运行时镜像（`Dockerfile.envd` / `Dockerfile.control-plane`）目前都安装了
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

依赖变化：`Dockerfile.envd` 移除 `docker.io docker-cli`；compose / k8s
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

一期控制面 `Dockerfile.control-plane` 移除 `docker.io docker-cli`；模板构建
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
- `scripts/build-images.sh` 默认 `TAG` 改为上述 registry 前缀，`PUSH=1`
  才推送。

## 5. 请求队列（容量不足时有界等待）

### 5.1 语义与目标

- 现状：容量不足立即返回 503 "No resources available"，扩容只能靠提前量，
  突发流量会直接失败。
- 目标：创建请求在容量不足时进入有界等待队列，autoscaler 按队列深度快速
  扩容；请求在超时或队满时返回 503，不做无限等待。

### 5.2 位置与存储

- 队列挂在控制面创建流程上：`registry.create` 之后、`select_and_reserve`
  失败时入队，保留 template / snapshot / volume 亲和参数与已生成的
  `sandbox_id`（记录状态置 `pending`）。
- 控制面是多副本部署（Redis 已支持），队列必须 **Redis-backed**：
  - 用 Redis ZSET（score = 到期时间）存储，便于按 deadline 取队首和超时
    淘汰；或 LIST + 重试循环，推荐 ZSET；
  - 每个控制面副本运行 Dispatcher 协程：按 `E2B_SANDBOX_QUEUE_POLL_S`
    周期弹出到期项 -> 尝试 `select_and_reserve` -> 成功则继续 provision；
    失败则按剩余 deadline 重新入队或淘汰；
  - 创建请求本身长轮询等待结果（HTTP 挂起），结果通过 Redis pub/sub 或
    状态轮询返回；客户端断开时丢弃对应队列项。
  - 半成品回滚：客户端在 provisioning 开始后断开 / 超时，处理协程必须
    删除已建沙箱（调 worker `/agent/sandboxes/{id}`）并释放节点配额，
    杜绝孤儿沙箱（SDK 侧影响与约束见 5.6）。

### 5.3 队列参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `E2B_SANDBOX_QUEUE_CAPACITY` | 200 | 队列上限，满则立即 503 |
| `E2B_SANDBOX_QUEUE_TIMEOUT_S` | 30 | 单请求最大等待，超时 503 |
| `E2B_SANDBOX_QUEUE_POLL_S` | 1 | Dispatcher 重试周期 |

### 5.4 公平性与亲和

- FIFO + 超时淘汰；
- 保留 volume / snapshot 节点亲和：入队项携带 `volume_node_id`，重试仍走
  同一亲和路径；目标节点 draining 或已被移除时直接失败返回（避免无限等待）；
- draining 节点不会获得新调度（调度器过滤），排队项同样不会落到
  draining 节点。

### 5.5 与 autoscaler 的关系

- 队列深度是"请求容量"最直接的信号：`/internal/fleet/metrics` 返回
  `queueDepth`，autoscaler 以"剩余容量 + 队列深度 + 503 率"组合作为主指标；
- Dispatcher 只负责重试投递，不负责扩容；扩容由 autoscaler 按指标驱动，
  避免同一组件既消费队列又扩缩容的耦合。

### 5.6 SDK 行为影响与约束

基于官方 e2b SDK 2.46.0 源码核实（本项目测试锁定的版本；JS SDK 2.46.1
为同一套 REST 语义）：

- **协议无感**：仍然是同一个 `POST /sandboxes`，排队成功返回 201、超时 /
  队满返回 503；SDK 的调用方式、参数、返回结构均不变。
- **SDK 无客户端重试**：503 映射为 `SandboxException`（429 ->
  `RateLimitException`，401 -> `AuthenticationException`）；SDK 不读
  `Retry-After` 也不自动重试，排队逻辑只能做在服务端。
- **请求超时是硬约束**：SDK 默认请求超时 60s（`REQUEST_TIMEOUT`，可用
  `request_timeout` 参数覆盖）。必须满足
  `E2B_SANDBOX_QUEUE_TIMEOUT_S + 扩容冷启动预算 < 60s`；不满足时调小
  队列超时，或在使用文档中要求调用方显式传 `request_timeout`（如 90~120s）。
- **孤儿沙箱防护**：客户端超时 / 断开后若已进入 provisioning，仅丢弃队列
  项不够，必须回滚（删除已建沙箱 + 释放配额），见 5.2。
- **重试提示**：客户端超时后盲目重试会叠加并发（thundering herd）且可能
  同时存在两个沙箱；建议调用方配合退避策略，或引入幂等键（当前服务端
  未支持，列为可选增强）。

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
| `E2B_AS_QUEUE_THRESHOLD` | 5 | 队列深度超过即扩（与利用率互为补充） |
| `E2B_AS_SCALE_UP_COOLDOWN_S` | 60 | 扩容冷却 |
| `E2B_AS_SCALE_DOWN_COOLDOWN_S` | 600 | 缩容冷却 |
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
- 运行参数与现状一致：`SYS_ADMIN`、`seccomp=unconfined`、
  `--sysctl net.ipv4.ip_unprivileged_port_start=0`、共享 workspace 卷挂载；
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
- 权限：`securityContext.capabilities.add: [SYS_ADMIN]`、
  `securityContext.seccompProfile.type: Unconfined`（K8s 1.19+）；
- 低端口：优先给 `NET_BIND_SERVICE` capability（替代
  `net.ipv4.ip_unprivileged_port_start=0` 这个 unsafe sysctl）；若仍走
  sysctl 方案，需 kubelet `--allowed-unsafe-sysctls` 放行；
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

1. 节点连续 `E2B_AS_IDLE_BEFORE_DRAIN_S` 秒利用率 = 0；
2. 调 drain 后活跃沙箱数 = 0；
3. 全局副本数仍 >= min_replicas；
4. 缩容冷却已过。

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
4. 模板镜像 CI 预推仓库，worker 首拉由 registry CDN 加速（Aliyun ACR）。

## 11. 落地路线图与改动清单

### Phase 0：仓库与镜像基建

- `scripts/build-images.sh` 默认 TAG 指向
  `registry.cn-shanghai.aliyuncs.com/byteplan/e2b-sandlock`；
- `.env.example` 补充 ACR 仓库地址与凭据占位；CI 增加 buildx push 任务。

### Phase 1：去 Docker socket

- `envd_service/runtime/image_resolver.py`：重写为 registry v2 直拉
  （第 4.2 节），保留 `resolve_image_rootfs` 签名；
- `Dockerfile.envd` / `Dockerfile.control-plane`：移除 docker CLI；
- `docker-compose*.yml`：移除 socket 挂载；worker / control-plane 环境变量
  指向 ACR；
- 单元测试：本地起一个最小 OCI 假仓库（Python http.server 实现 manifest/
  blob），覆盖白 out / 不透明目录 / 鉴权 / 重定向 / 并发锁；
- `scripts/smoke-prod-worker.sh`：去掉 docker socket 前置说明，改为验证
  registry 直拉路径。

### Phase 2：控制面扩缩容能力

- `control_plane/registry/nodes.py`：draining 字段 + 过滤；
- `control_plane/scheduler.py`：排除 draining；
- `control_plane/api/internal.py`：drain 接口 + fleet metrics 聚合接口；
- 新增 `control_plane/queue.py`（或 `registry/queue.py`）：Redis ZSET 有界
  队列 + Dispatcher 协程；
- `control_plane/api/sandboxes.py`：创建流程接入队列（容量不足 -> 入队
  长轮询，队满/超时 -> 503，客户端断开 -> 清理）；
- 单测：draining 不调度、metrics 聚合正确性、队列入队/超时/队满/亲和/
  断开清理。

### Phase 3：Autoscaler + 本地后端

- 新增 `autoscaler/`：`policy.py` / `loop.py` / `backends/local.py`；
- 新增 `docker-compose.autoscale.yml`（control-plane + gateway + autoscaler +
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
   503（阈值内），压力消失后缩回 min；
3. 容量不足时请求入队等待而非立即 503；autoscaler 按队列深度扩容后
   请求成功，超时 / 队满才返回 503；
4. SDK 视角：排队场景下 create 要么 201 成功、要么 503 / 超时，且客户端
   超时 / 断开后无遗留沙箱（无孤儿）；
5. K8s 模式：同一套决策逻辑驱动 Deployment 副本数，worker pod 自动注册
   到控制面并被 gateway 正常路由；
6. 缩容全程活跃沙箱存活率 100%（drain 后新请求不落该节点）；
7. 全部既有测试通过（新 resolver 用假仓库单测覆盖，不依赖宿主机 docker）。

## 13. 可选增强（暂不进入一期）

- 模板构建 Builder 服务（BuildKit / kaniko），恢复"控制面触发构建"能力；
- HPA + custom metrics 原生扩缩容；
- cluster-autoscaler 节点级伸缩。
