# Task Z 报告：本地部署测试（Z1 起栈 + Z2 部署冒烟）

**状态：DONE（Z1/Z2 全绿；产出 2 个部署文件修复提交；另有 1 条口径冲突待你拍板）**

| 项 | 值 |
|---|---|
| 主仓库 | `main` @ `3b04f93`（本轮新增提交 `2cb85fb`、`5df7367`，显式 pathspec，未 amend） |
| fork 子模块 | 只读，tip `a063daf`（未改动） |
| 测试镜像 | `e2b-sandlock-test:latest` = `f28b65e87fb0`（未重建，仅作为 `smoke-prod-worker.sh` 的载体） |
| 本轮本地镜像 | worker `57a1cd74b94a` / control-plane-gateway `e0bc44ece895`（修前）、`db4e5514bb86`（修后 r1）/ quota-agent `de6d2adb6352` / autoscaler `76ce1f5d348e`，tag `z1-3b04f93`（+`-r1`），**全部本地构建、未推 ACR** |
| 部署形态 | `deploy/stack/docker-compose.prod.yml` 起栈：control-plane（合并 gateway）+ 2×worker + redis + buildkit（`docker compose ps` 五个容器 Up；redis healthy） |
| 主机端口 | `CONTROL_PLANE_PORT=3900`（本机 3000 被无关 node 进程占用；只改宿主发布端口，栈本身未动） |
| 工作区 | `/Users/polus/project/ai/sandlock-e2b`，`git status`：`?? target`（既有）+ 本轮 2 个部署文件改动（已提交） |

## 0. 结论（先看这三行）

1. **部署形态跑通了**：真 compose 栈 + 真镜像 rootfs + 真 route-B 槽位下，
   `deployment_smoke.py`、`multinode_smoke.py`、官方 SDK 手工复核（`pwd == /home/user`、写读回显）
   全部 `EXIT=0`；worker 全程 **零 SYS_ADMIN**（`CapEff` 实测、日志零命中）。
2. **但 route-B 只在 root worker 上成立**：出厂清单把 worker 钉在 `user: "65534:65534"`，
   envd 因此自动关闭 per-sandbox uid（`PER_UID_NONROOT_WARNING`），route B 被静默降级 ——
   与 §2.4「route-B 已默认开」以及线上审计的「worker 实际是 root」**互相矛盾**（F1，需你拍板）。
   本报告因此跑了两种形态，两种都记录在案。
3. **顺手修掉两个"冷启动即不可用"的部署缺口**（F2/F3，仅部署文件，未加任何特权），
   否则"用 compose 起栈"这一步本身就走不到建箱。

## 1. Z1：本地 compose 起栈

### 1.1 构建（本地 tag，未推 ACR）

| 日志 | 命令 | 结果 |
|---|---|---|
| `tmp/z1-build.log` | `REGISTRY=e2b-local PLATFORMS=linux/amd64 VERSION=z1-3b04f93 ./deploy/scripts/build-images.sh` | `EXIT=0`（worker / autoscaler / quota-agent） |
| `tmp/z1-build-cp.log` | `docker buildx build --load -f deploy/docker/Dockerfile.control-plane-gateway -t e2b-local/e2b-sandlock-control-plane-gateway:z1-3b04f93 .` | `EXIT=0` |
| `tmp/z1-build-cp-fix1.log` | 同上，修复 F2 后重建为 `…:z1-3b04f93-r1` | `EXIT=0` |

镜像自检（无 `SYS_ADMIN` 前提的 route-B 语言面）：

```
uid=65534(nobody)  /usr/local/lib/python3.14/site-packages/sandlock/__init__.py
sandlock/bin/  -> ['sandlock-supervise']      # route B 需要的槽位二进制在镜像里
```

### 1.2 起栈 + `ps`

`tmp/z1-compose.log`（首轮）、`tmp/z1-compose-fresh.log`（清掉陈旧卷后的"冷启动"复跑）、
`tmp/z1-compose-root.log`（root worker 形态）、`tmp/z1-compose-quota.log`（quota profile）全部 `EXIT=0`。

```
stack-control-plane-1   Up   0.0.0.0:3900->3000/tcp
stack-worker-1-1        Up   49983/tcp
stack-worker-2-1        Up   49983/tcp
stack-redis-1           Up   (healthy)
stack-buildkit-1        Up
```

### 1.3 `./deploy/scripts/smoke-prod-worker.sh`：**失败，且是脚本过时**（F9）

出厂形态直接跑：`tmp/z1-worker-smoke.log` = `12 warnings, 3 errors`，`EXIT=1`，
三条用例全部停在 `tests/security/conftest.py` 的 `require_sandlock` 探针
`sandlock_create failed`。

定位（不是本轮改动引入的产品缺陷）：

- 该脚本 2026-08-30 定稿，**早于** E5.1（`35fefdd`，2026-09-02，worker 改非 root）与 A7
  的"无 SYS_ADMIN 固化"。它假设"非特权容器"，但 `e2b-sandlock-test` 镜像没有 `USER` 声明，
  `docker run` 实际以 **root + Docker 默认 cap 集**（无 `SYS_PTRACE`）运行——既不是测试 lane
  的 root+ptrace 形态，也不是部署的 uid 65534 形态：纯形态进程内 RunAs 写不了子进程
  `uid_map`，于是 `sandlock_create failed`。
- 换成部署身份复跑（`--user 65534:65534 --cap-drop ALL -e HOME=/tmp`，
  `tmp/z1-worker-smoke-uid65534.log`）= `1 failed, 1 passed, 1 skipped`：建箱成功，
  `test_egress_proxy_tunnels_tcp_after_filter` 通过；失败的是
  `test_sandbox_child_runs_unprivileged`，它断言沙箱内是 `0 0`（旧共享 uid 1000 + RunAs 语义），
  而部署形态下沙箱就是 worker 自己（实测 `65534 65534`）；skip 的那条要 docker daemon
  （该变体没挂 socket）。
- **结论**：脚本 + 该用例的期望都没跟上 E5.1/route-B 的语义。改它要动"沙箱内到底是
  uid 0 还是 worker uid"的产品口径 ⇒ **留给用户拍板，本轮未改**（也未用加 `SYS_PTRACE`
  之类的方式把它"弄绿"）。

## 2. Z2：部署冒烟

两种形态都跑；`E2B_API_URL = E2B_SANDBOX_URL = http://127.0.0.1:3900`，
`E2B_API_KEY=local-key`，`E2B_INTERNAL_API_KEY=internal-key`（本地临时值）。

### 2.1 形态 A：出厂清单（worker `user: "65534:65534"`，无任何 cap，`CapEff=0`）

| 项 | 日志 | 结果 |
|---|---|---|
| `deployment_smoke.py` | `tmp/z2-deploy-smoke.log` | `EXIT=0`：跨 worker 分布、命令/文件、共享卷迁移、network 回显+原子更新、卷挂载+兄弟卷隔离、模板构建→registry push→worker pull→镜像 rootfs、MCP 网关 + streamable HTTP；kill 后配额归零 |
| `multinode_smoke.py` | `tmp/z2-multinode-smoke.log` | `EXIT=0`：4 沙箱 2+2 分布、命令/文件/health/stdin、kill 后归零 |
| SDK 手工复核 | `tmp/z2-sdk-manual.log` | `EXIT=0`：`pwd` = **`/home/user`**、`echo hi > /home/user/a.txt && cat` = `hi`、`files.write/read` 一致、`/workspace/rel.txt` 别名同文件、沙箱内 `uid=65534`（= worker 身份，符合 §2.4「非 root worker 保持固定身份」） |

worker 日志：`tmp/z1-worker-logs-default.log` —— 启动即 `PER_SANDBOX_UID is enabled but the
worker is not running as root; per-sandbox host uids are disabled`，**route-B 就绪行 0 条**；
`SYS_ADMIN` 命中 0；`CapEff = 0000000000000000`。
即：这一形态的部署**功能可用但完全不走 route B**。

### 2.2 形态 B：root worker（**不添加任何 cap**，与线上审计形态一致）

用 `tmp/z1-compose-root.yml`（只覆盖 `user: "0:0"` + 两个 worker 的**互不重叠** uid 段
`E2B_UID_POOL_START/SIZE` = 10000/100 与 10100/100）叠加同一个 stack 文件起栈；
Docker 对 root 的默认 cap 集实测 `CapEff=00000000a80425fb` —— **不含 `SYS_ADMIN`（bit21）
也不含 `SYS_PTRACE`（bit19）**。

| 项 | 日志 | 结果 |
|---|---|---|
| route-B 证据 | `tmp/z1-routeb-evidence.log` | `EXIT=0`：两个沙箱分别落 worker-1/worker-2，`pwd=/home/user`；**进程表里 `sandlock-supervise --policy /tmp/sandlock-route-b/<uid>/<sbx>/policy.json --uid <uid>` 正跑在该沙箱的 host uid 上**（worker-1 `uid=10001`、worker-2 `uid=10100`，与 `sandbox.json` 的 `host_uid` 逐一对应；槽位目录 `/tmp/sandlock-route-b/10001/…`、`/tmp/sandlock-route-b/10100/…` 分属两段）；收尾 kill 后 `reserved = 0` |
| `deployment_smoke.py` | `tmp/z2-deploy-smoke-root.log` | `EXIT=0`（六段全 OK） |
| `multinode_smoke.py` | `tmp/z2-multinode-smoke-root.log` | 第 1 次撞上 ACR 偶发 TLS EOF 红（F10），**重试 `EXIT=0`**（2+2 分布、stdin OK、归零） |
| SDK 手工复核 | `tmp/z2-sdk-manual-root.log` | `EXIT=0`：`pwd=/home/user`、`hi`、别名同文件；沙箱内 `uid=0`（route-B 槽位自映射 0→host uid，与 §2.4.1 实测一致） |
| worker 日志 | `tmp/z1-worker-logs-routeb.log` | `SYS_ADMIN` 命中 **0**；`CapEff=00000000a80425fb`；route-B 就绪行 **0 条**（见 F4） |

### 2.3 无 `SYS_ADMIN` 的对照（本轮 Track A 的收口证据）

| 进程 | uid | `CapEff` | 含 SYS_ADMIN? |
|---|---|---|---|
| worker（出厂清单形态） | 65534 | `0000000000000000` | 否（一个 cap 都没有） |
| worker（root 形态，**未加任何 cap**） | 0 | `00000000a80425fb` | **否**（Docker 默认集；SYS_ADMIN 位 = 0） |
| quota-agent（`--profile quota`） | 0 | `00000000a82425fb` | **是**（= 默认集 + SYS_ADMIN，`0x200000`，特权只留在这里） |

worker 日志里 `SYS_ADMIN` 命中：**0**（`tmp/z1-worker-logs-default.log`、
`tmp/z1-worker-logs-routeb.log`、`tmp/z1-quota-logs.log` 的 count 字段）。
三处原用途（共享卷 bind / 本地 `xfs_quota` / 低端口 sysctl）在本轮部署形态下**都没有回来**：
bind 由 A4/A5 的方案取代、sysctl 由容器 spec 声明（compose `sysctls:`）、配额走 HTTP agent。

### 2.4 quota profile 复跑（可选，已做）

`E2B_QUOTA_AGENT_URL=http://quota-agent:49984` + `--profile quota`（`tmp/z1-compose-quota.log`、
`tmp/z1-quota-logs.log`）：

- quota-agent 起来并服务 `GET /detect`、`POST /reconcile`（200）；worker 侧只发 HTTP；
- **无真 XFS 的降级行为符合预期**：worker 打
  `XFS project quota unavailable for /var/lib/e2b-sandboxes: filesystem is btrfs, not xfs`，
  建箱 / 挂卷照常（`pwd=/home/user`、`echo hi` 回显一致，`EXIT=0`），**不阻塞**；
- 本机容器确实造不出 prjquota（无 `/dev/loop-control`，卷是 btrfs），与 brief 的已知边界一致。

### 2.5 收栈

`tmp/z1-compose-down.log` = `docker compose -f deploy/stack/docker-compose.prod.yml --profile quota down`，
`EXIT=0`，`stack-*` 容器 0 个残留；命名卷保留（日志与卷都在，未 `-v`）。

## 3. 发现（按严重度）

### F1【需你拍板】出厂清单与「route-B 默认」互相矛盾

- 事实：`deploy/stack/docker-compose.prod.yml` 的 worker 是 `user: "65534:65534"`；
  envd 对非 root worker **自动关闭 per-sandbox uid**（实测启动 WARNING），而 route B
  的硬前置就是 per-sandbox host uid ⇒ **route B 在出厂清单下不可能触发**
  （形态 A 全绿但一条 route-B 就绪行都没有；形态 B 一加 root 就全都有了）。
- 同时：§2.4.1 的线上审计写明"仓库清单 `user: 65534` ≠ 已部署状态，**线上 worker 实际是 root**"。
  也就是说"route-B 默认开"这句只在 root worker 上成立，而仓库交付的 compose 交付的是另一种形态。
- 影响面：非 root 形态下沙箱是 worker 身份（65534），**同 worker 内沙箱之间没有 DAC 隔离**
  （§2.4 的 1777+sticky 保护与 T5 的"写者属主 = 沙箱"都依赖 per-sandbox uid）；
  root 形态下才有 §2.4 描述的全部性质。
- 需要你选：① 清单去掉 `user:`（回到 root + 无 cap，与线上一致）；② 保留 65534 并把
  §2.4/§2.4.1 的"默认"改成"非 root 形态不启用 route B"；③ 其它（例如给 worker 补
  `CAP_SETUID/SETGID/CHOWN/SYS_PTRACE` 仍以 uid 65534 跑 —— 这条路本轮**没有**尝试，
  因为它需要非 root 进程拿到 effective cap，Docker 对非 root 会清空 `CapEff`，多半不可行）。

### F2【已修 `2cb85fb`】冷卷首启：控制面先占共享卷 ⇒ worker 建箱 EACCES

- 现象：`docker compose up` 后第一次建箱，worker 日志只有 `POST /agent/sandboxes 401`，
  control plane 回 `502 Node worker-2 failed to provision: `（**空消息**）；容器内直调
  `_agent_create_sandbox` 才看到真因：
  `PermissionError: [Errno 13] Permission denied: '/var/lib/e2b-sandboxes/sbx_zprobe1'`。
- 根因：control plane 是共享卷的**第一个挂载者**（worker `depends_on: control-plane`），
  而它的镜像里没有 `/var/lib/e2b-sandboxes`，Docker 就把新卷初始化成 `root:root`；
  control plane 启动时又立刻在里面建 `_secrets`/`_templates`，于是 uid 65534 的 worker
  永远写不进去。`upgrade.sh` 的一次性 `chown` 迁移只覆盖"已存在的卷"。
- 修法：`deploy/docker/Dockerfile.control-plane-gateway` 里预建该目录并 `chown 65534:65534`
  （与 worker 镜像同一口径；control plane 自己是 root，不受影响）。
- 证据：`tmp/z1-deploy-fix-probes.log` §A —— 修前镜像 `mkdir: FAILED (EACCES)`，
  修后镜像 `mkdir: OK`；清掉陈旧卷后的 `tmp/z1-compose-fresh.log` 冷启动即建箱成功。
- 附带说明：为排除陈旧状态干扰，我删除了本机两个**本轮之前的**栈卷
  `stack_sandbox-shared`（2026-09-02 建、内容为空）与 `stack_redis-data`（内含一条
  2026-09-02 遗留的配额占用：worker-1 `512MB/100cpu/1024disk/64proc`）。两者都是本机旧
  运行产物，不在仓库里，删后由 compose 重建。

### F3【已修 `2cb85fb` + 口径更正 `5df7367`】worker 缺 `E2B_IMAGE_REGISTRY`

- 事实：`deploy/stack` 的 worker env 有 `E2B_IMAGE_REGISTRY_USERNAME/PASSWORD`，**没有**
  `E2B_IMAGE_REGISTRY`（`deploy/compose/docker-compose.prod.yml` 的 worker 是有的）。
- 真实后果是**凭据不再按 host 收窄**：`registry_credential_host()` 返回 `None` ⇒
  `RegistryClient` 的 host 校验被短路，ACR 的用户名/口令会被附到**任何** host 的请求上
  （实测 `python:3.14-slim` 走 `registry-1.docker.io` 时 `credential attached: True`）。
  这正是 `oci_registry.py` docstring 里要避免的泄漏。
- **口径更正**：我最初把首轮 `428 warm_required` 归因于此，**这是错的**。
  后续实测：带/不带该变量的 `resolve_image_rootfs(<ACR 镜像>)` 都成功；
  真正的 428 是 ACR token 端点在两个 worker 同时预热时偶发丢 TLS 握手（F10），重试即过。
  更正写在 `5df7367`（未 amend `2cb85fb`，保留纠正痕迹），compose 注释也已改成"scoping"口径。
- 证据：`tmp/z1-deploy-fix-probes.log` §B/§C。

### F4【未修】`route-B instance ready …` 在部署形态的日志里**看不到**

- 该行是 `logger.info`（`envd_service/executors/sandlock.py`）；worker 进程里
  `uvicorn.run(log_level=…)` 只配置 `uvicorn*` 这几个 logger，`envd_service.*` 继承**root
  logger = WARNING**，所以 INFO 一律被丢弃（连 `worker image warmed` 这种 INFO 也不见）。
  实测：`E2B_LOG_LEVEL=DEBUG` 也不改变这点（`tmp/z1-worker-logs-routeb.log` 的
  `route-B readiness lines count=0`）。
- 影响：brief 的"worker 日志里应出现 route-B 就绪行"这条**在当前部署配置下无法用日志满足**；
  本轮改用**强于该行**的证据：槽位进程表（`sandlock-supervise --uid <host_uid> …`）+
  `sandbox.json.host_uid` + 槽位目录（`tmp/z1-routeb-evidence.log`）。
- 建议修法（未做，属产品可观测性口径）：`envd_service/__main__.py` /
  `control_plane/combined_main.py` 里加 `logging.basicConfig(level=settings.log_level.upper())`，
  或给 `envd_service` logger 配 handler。

### F5【未修】冷节点首个 create 直接 428，而官方 SDK 不会带 `X-Sandbox-Id`

- control plane 的冷镜像路径要求 `X-Sandbox-Id` 头才走"先预热再建箱"（`sandboxes.py:1058`），
  但 e2b SDK（2.46.0）只在**响应之后**才知道 `sandbox_id`，create 请求不带该头 ⇒
  冷窗口内用户看到的是 `428 warm_required`，没有任何自动重试。
- 本轮靠"worker 启动预热完成后再跑冒烟"规避（`_warm_base_image` 是启动任务）。
  建议：要么 control plane 在冷路径上自己排队/预热（不依赖头），要么在文档里把
  "预热完成前不要放流量"写成硬前提。

### F6【未修】provisioning 的 `PermissionError` 被当成 401

- `envd_service/agent.py::agent_create_sandbox` 用 `except PermissionError: return 401`
  同时兜住了"鉴权失败"和"建箱过程中的 EPERM/EACCES"，响应体为空；control plane 于是只打
  `502 Node … failed to provision: `（空串）。F2 就是被这层掩盖的（排查时必须进容器直调）。
- 建议：鉴权在那儿单独判定，建箱的 `PermissionError` 归到 500/507 并带原因。

### F7【未修】worker 重建即丢镜像缓存

- 镜像缓存落在容器内 `/app/tmp/sandboxes/_images`（**不是**共享卷），每次
  `up -d`/重建容器后都要重新拉取+解包基镜像（本轮实测冷窗口 20–120s）。窗口内建箱即 F5 的 428。

### F8【未修】默认容量只能放 1 个沙箱/worker

- `E2B_NODE_PROCESSES=256`（stack `.env`）而 `E2B_DEFAULT_MAX_PROCESSES` 默认也是 256 ⇒
  按 `can_fit` 每个 worker 只能放 **1** 个沙箱；两个冒烟脚本分别要 3 个和 4 个沙箱并断言
  跨两节点分布。本轮在**本地 .env** 把每节点容量调到 `4096MB/400%/8192MB/1024proc`
  （单沙箱上限不变），才让脚本成立。
- 这是"出厂默认值与自带冒烟脚本不自洽"，建议要么调默认值，要么在文档/脚本里写明
  跑冒烟前需要的容量配置。

### F9【已修（2026-09-12，fix round 2）】`smoke-prod-worker.sh` 形态过时

> 已关闭：脚本改为部署形态（`--user 65534:65534 --cap-drop ALL --cap-add SETUID,SETGID,CHOWN,DAC_OVERRIDE`），
> `test_sandbox_child_runs_unprivileged` 按已拍板口径改写为 route-B 身份（宿主 uid = 池内 uid、ns 内 = uid 0）。
> 实测非 root 形态 `2 passed, 1 skipped, EXIT=0`，见 `.superpowers/sdd/task-F1-report.md`「Fix round 2」与
> `tmp/f1/f1-c2-smoke-final.log`。下面保留原始记录。

见 §1.3。脚本 2026-08-30 定稿，早于 E5.1 非 root worker 与 A7 无 SYS_ADMIN 固化；
它的容器身份（root + 默认 cap）与部署身份（65534）都不一致，且其中一条用例断言的是
旧共享 uid 语义。修它要动产品口径 ⇒ 交给你决定。

### F10【环境】ACR 偶发 TLS EOF；陈旧 Redis 配额残留

- `[SSL: UNEXPECTED_EOF_WHILE_READING]` 在 ACR token 端点（`dockerauth.cn-hangzhou…`）
  偶发出现（同一时刻 5 次重试即 5 次 200），表现为 worker 预热/解析失败、建箱 428，
  在并发建箱时更易触发（`tmp/z2-multinode-smoke-root.log` 首跑）。这与 D2 把测试侧默认
  改成"本地 registry 预置镜像全集"的动机一致；部署形态用公共/私有 registry 时这属环境噪声。
- 旧 `stack_redis-data` 里有一条 2026-09-02 的遗留占用（worker-1 512MB/64proc），
  会让 `can_fit` 提前判满、报 `503 No resources available`；本轮清卷后复现不出，
  三套冒烟结束时 `reserved` 均为 0（未见新增泄漏）。

## 4. 复现入口（本机）

```bash
# 1) 本地构镜像（不推 ACR）
REGISTRY=e2b-local PLATFORMS=linux/amd64 VERSION=z1-3b04f93 ./deploy/scripts/build-images.sh
docker buildx build --load --platform linux/amd64 \
  -f deploy/docker/Dockerfile.control-plane-gateway \
  -t e2b-local/e2b-sandlock-control-plane-gateway:z1-3b04f93-r1 .

# 2) 本地 .env（临时值；原件备份在 tmp/z-env-backup.target-host，本地跑法副本 tmp/z-env-track-z.local）
#    CONTROL_PLANE_PORT=3900 / *_IMAGE=e2b-local/… / E2B_API_KEYS=local-key /
#    E2B_INTERNAL_API_KEY=internal-key / E2B_BASE_IMAGE=<ACR python-mcp:3.14> /
#    每节点容量 4096MB/400%/8192MB/1024proc

# 3) 起栈（默认形态） / root-worker 形态 / quota 形态
docker compose -f deploy/stack/docker-compose.prod.yml up -d
docker compose -f deploy/stack/docker-compose.prod.yml -f tmp/z1-compose-root.yml up -d
docker compose -f deploy/stack/docker-compose.prod.yml --profile quota up -d

# 4) 冒烟
env E2B_API_URL=http://127.0.0.1:3900 E2B_SANDBOX_URL=http://127.0.0.1:3900 \
    E2B_API_KEY=local-key E2B_INTERNAL_API_KEY=internal-key \
    tmp/z-venv/bin/python deploy/scripts/deployment_smoke.py
# multinode_smoke.py / tmp/z2-sdk-manual.py 同理

# 5) 收栈
docker compose -f deploy/stack/docker-compose.prod.yml --profile quota down
```

（`tmp/z-venv/` 是只装了 `e2b==2.46.0 / mcp==2.1.1 / httpx2 / httpx` 的临时 venv；
本机 `python` 不在 PATH 上。）

## 5. 证据文件（均以 `ENV-HEADER` 开头、`EXIT=` 结尾）

| 文件 | 内容 |
|---|---|
| `tmp/z1-build.log`、`tmp/z1-build-cp.log`、`tmp/z1-build-cp-fix1.log` | 三个镜像的本地构建 |
| `tmp/z1-compose.log`、`-fresh`、`-root`、`-quota`、`-down` | 起栈 / 冷启动复跑 / root 形态 / quota 形态 / 收栈 |
| `tmp/z1-worker-smoke.log`、`tmp/z1-worker-smoke-uid65534.log` | `smoke-prod-worker.sh` 出厂形态失败 + 部署身份变体 |
| `tmp/z1-worker-logs-default.log`、`tmp/z1-worker-logs-routeb.log` | 两形态的 worker 全量日志 + CapEff + SYS_ADMIN/route-B 计数 |
| `tmp/z1-routeb-evidence.log` | route-B 槽位进程表、槽位 uid ↔ `sandbox.json.host_uid`、槽位目录、收尾归零 |
| `tmp/z1-quota-logs.log` | quota-agent 日志 + 三角色 CapEff + worker 降级告警 |
| `tmp/z1-deploy-fix-probes.log` | F2/F3 的可复现探针（冷卷所有权、凭据归属/是否外泄） |
| `tmp/z2-deploy-smoke.log`、`tmp/z2-deploy-smoke-root.log` | 部署冒烟（两形态） |
| `tmp/z2-multinode-smoke.log`、`tmp/z2-multinode-smoke-root.log` | 多节点冒烟（两形态；root 形态第 1 跑红=F10，重试绿） |
| `tmp/z2-sdk-manual.log`、`tmp/z2-sdk-manual-root.log` | 官方 SDK 手工复核（两形态） |
| `tmp/z-env-track-z.local`、`tmp/z-env-backup.target-host` | 本地跑法 .env 副本 / 目标机 .env 备份（`deploy/stack/.env` 已还原为目标机版本） |
| 辅助脚本 | `tmp/z-run.sh`、`tmp/z-capture-worker-logs.sh`、`tmp/z1-routeb-evidence.sh`、`tmp/z1-deploy-fix-probes.sh`、`tmp/z1-compose-root.yml`、`tmp/z2-sdk-manual.py` |

## 6. 未决事项

1. **F1**（uid 65534 vs route-B 默认）：需要你选口径，我未改清单。
2. **F9**（`smoke-prod-worker.sh`）：需要先定"沙箱内身份"的产品口径，再把脚本和用例一起对齐。
3. **F4/F5/F6/F7/F8**：四项可观测性/冷启动/容量默认值问题，都已给修法建议，本轮未动代码。
4. 本轮两个提交只碰部署文件（`deploy/docker/Dockerfile.control-plane-gateway`、
   `deploy/stack/docker-compose.prod.yml`），未触碰 runner / 产品代码 / fork；
   `deploy/stack/.env` 已还原，未被提交（gitignored）。
5. `tmp/` 下本轮新文件较多（约 20 个日志 + 4 个辅助脚本 + 1 个 venv）；venv 可随时删，
   日志建议留到最终评审后一并清理。
