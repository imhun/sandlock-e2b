# C3 compose 车道：三条已知缺口的收口（含做验收时量到的两个真缺陷）

- 工作目录 `/Users/polus/project/ai/sandlock-e2b`，worktree `tmp/wt-c3-compose`，
  分支 **`feat/c3-compose-gaps`**（从 `main` 的 C3 合并点 `a166548` 起）
- 未 push、未 merge、未碰 k0s 集群；所有现场读数来自**本机 multinode 栈**（OrbStack，
  linux/amd64），镜像全部从**本树**构建（`:c3-gaps`）
- 原始日志/读数：`tmp/wt-c3-compose/tmp/c3-compose-gaps/`（`evidence-*.txt`、`*.log`、`logs/`）

> **评审轮（2026-09-30，同一分支）**：评审确认三条缺口与两个额外修复都落地，但**这一笔
> CP-uid 改动**在我上一轮**没有起过**的 `deploy/stack` 上带出两条回归（buildkit 的 unix
> socket 读不到、TLS 配方把 CP 打死），另有若干注释/文档/钉子问题。逐条收口与两条新的现场
> 读数见 **§6**（本节 §0–§5 保留为上一轮的原始记录，里面的数字是那一次的读数）。

## 0. 结论

| 缺口 | 结论 | 一句话 |
|---|---|---|
| ① compose CP 仍是 root | **收口** | 三个栈的 `control-plane` 都成了 `65534:65534`；栈里剩下的 root 恰好两个（`image-cache-init`、面 B `c3-agent-maint`），且都必需。D24 的卷存储交棒进了 `image-cache-init`（非递归/幂等/逐目标校验/具名 FATAL）；实测卷创建 **201**，`_volumes` 与 `_meta` 都是 `65534:65534`。 |
| ② 没有策略层 | **收口（名字层 + 凭据层；IP 层依赖 daemon）** | 三个栈都加了 `agent-plane` 专用网络：只有两个 agent 面与 CP 在上面，worker 留在默认网络 ⇒ worker **解析不到** agent 的服务名。⚠ 本机 OrbStack 不实现跨网隔离（上游已知行为，见 §5），所以本机**按 IP 直连 agent 是通的**；标准 Docker daemon 会丢这条包。 |
| ③ multinode 没 Redis | **收口** | 加了 `redis` 服务与 `E2B_REDIS_URL`，面 B 打开扫描；门 (a) 不再推迟，轮次日志 `… deferred=-`。 |
| ④ **验收中量到的新缺陷**：compose 自愈上报被源 IP 因子整条拒 | **收口** | 面 B 上报、期望值却只取面 A 的地址 ⇒ 三个 compose 栈的自愈**全部**失效（`403 … came from 192.168.117.3, expected 192.168.117.2`）。期望值改取"两个面地址的并集"。 |
| ⑤ **验收中量到的新缺陷**：agent 入口不放开 INFO | **收口** | 成功轮次那一行是 INFO、被拒轮次是 WARNING ⇒ "自愈活着吗"的那一行恰好在健康态看不见。入口逐条照抄 worker/CP 的做法。 |

## 1. 提交

| commit | 内容 |
|---|---|
| `fb9ec50` | `feat(compose): C3 的三条 compose 缺口收口（CP 65534 / agent-plane / Redis）` —— 三个 manifest + 清单钉子 |
| `1533d0f` | `fix(c3): compose 自愈上报按两个 agent 面的地址收` —— 缺陷 ④ |
| `d5b26f6` | `fix(c3-agent): agent 入口放开 INFO，让自愈轮次那一行可见` —— 缺陷 ⑤ |
| `ccab1e3` | `docs(c3): §11.2.1 三条 compose 缺口收口 + deploy-clusters §7.11 现场读数` |
| `<本报告>` | `chore(c3): 固化 compose 车道收口的验收报告`（`git add -f`，与 C3 的其它报告同规矩） |

## 2. 逐条改了什么

### 缺口 ①：compose CP 收到 65534 + 属主交棒

**清单**（三个栈都改，逐条同形）：

* `control-plane` 服务加 `user: "65534:65534"`（= `deploy/k8s/control-plane.yaml` 的
  `runAsUser/runAsGroup: 65534`）。§13.6 的三条证据把 uid 钉死在 65534：平台自己的目录是 65534、
  `<state>/.uid_pool.lock` 是 `0600 65534`（换 uid 连开都开不了）、镜像缓存属主是 65534。
* `image-cache-init`（那个 root one-shot，compose 车道对应 k8s CP pod 的 init）扩成
  `deploy/k8s/c3-agent.yaml` 里 `storage-init` ① ② + `workspace-root-init` 的对应物：
  1. **`CACHE_DIRS`**：改成 `storage-init` 的谨慎形 —— 目录本身**非递归**交棒、只有 `_oci/`
     递归、`secrets/` 只动目录（`*.secret` 是活沙箱的文件，`chown -R` 会在每次 `up -d`
     把它们夺回 65534，运行中的沙箱就读不到自己的 secret 了）；
  2. **`OWNED_DIRS`**：CP 自己写的 `_builds/_secrets/_snapshots/_templates` + 树根带的
     `_snapshots/_migrate`（multinode 是两张卷：CP 的 `control-data` 与 worker 的 `worker-data`
     各有一套），逐个 `mkdir -p` → `chown` → **re-stat 校验** → 不合格即 FATAL + 一次性命令；
  3. **`WRITABLE_ROOTS`**：k8s `workspace-root-init` 的判定（属主 65534，或 1777），
     并点名 `Sandbox.create()` 会 EACCES；
  4. **`VOLUME_STORES`**：D24 的卷存储交棒 —— `hand_over` 一个目标一个目标地做，
     **只 chown 那个目录本身（永不 `-R`）**，三种分支各自一行（`does not exist` /
     `already belongs to uid 65534 (mode …)` / `-> uid … mode … (a non-recursive hand-over…)`），
     成功行在**门之后**打印；拒绝则 `FATAL: … is owned by uid N, not the control-plane uid 65534:
     the control plane creates every volume as <store>/<volume_id> and writes
     <store>/_meta/<volume_id>.json, so the first volume create would fail with EACCES` +
     `fix it once …`（与 k8s `storage-init` 的失败措辞同源）。
  * multinode 的挂载路径不同（`/cache/control`、`/cache/worker`），但**脚本正文三栈逐字相同**，
    只由 `CACHE_DIRS/OWNED_DIRS/WRITABLE_ROOTS/VOLUME_STORES` 驱动。
* 只有 CP 服务加 `user:`；`image-cache-init` 仍是 `0:0`（交棒必需），面 B 仍是 `0:0`（NFS `chown` 必需）。

**钉子**（`tests/unit/test_c3_cp_rootless.py` 新增一节）：

* 文本钉子：三栈都含 `hand_over() {` / `hand_over "$store" || exit 1` / `hand_over "$store/_meta" || exit 1` /
  门语句 / 失败措辞，且 `chown -R 65534:65534 "$store` 与 `… "$target` **不得出现**；
  `VOLUME_STORES` 必须指向 CP 真正写的那条路径（`<base>/_volumes`）。
* **行为钉子**：把三栈的 init 正文各自**真跑一遍**（compose 的 `$$` 还原成 `$`，用会记账的
  `stat`/`chown` shim 抹平 Linux/macOS 差异），三条臂 × 三个栈：
  ① 交棒成功（每个目标一行 `-> uid 65534 mode 755 …`，退出 0）；② 已属主 no-op（`already belongs …`）；
  ③ `_meta` 被拒 ⇒ 退出 1、**不打**成功行、stderr 三行逐字匹配（含一次性命令）。

### 缺口 ②：agent 面进专用网络 `agent-plane`

**清单**（三个栈）：顶层 `networks: {default: , agent-plane: {internal: true}}`；
`c3-agent` 与 `c3-agent-maint` 的 `networks: [agent-plane]`（**只这一张**）；
`control-plane` 的 `networks: [default, agent-plane]`；worker 一个都不加（留在默认网络，
`worker ↔ CP` 那条不变）。文件里用一段注释把这条性质命名出来（k8s 用 NetworkPolicy 表达的
"只有 CP 能进 agent 的 49985/49986"，compose 用拓扑表达）。

**钉子**（`tests/unit/test_c3_agent_manifest.py`）：
`test_the_compose_agent_channel_is_a_network_no_worker_joins` —— 三栈逐条断言
`agent-plane == {internal: true}`、两个面只在该网、CP 同时在两张网、**任何 worker 都不得出现在
`agent-plane`**，以及那段注释里两个通道名字仍在（读者要能一眼看见）。

### 缺口 ③：multinode 补 Redis

**清单**：`deploy/compose/docker-compose.multinode.yml` 新增 `redis`（`redis:8-alpine`、
`--appendonly yes --requirepass ${E2B_REDIS_PASSWORD:-local-redis-password}`、healthcheck、
`restart: unless-stopped`、`redis-data` 卷）；`control-plane` 加
`E2B_REDIS_URL: redis://:${E2B_REDIS_PASSWORD:-local-redis-password}@redis:6379/0` 与
`depends_on: redis: service_healthy`；面 B 加 `E2B_C3_AGENT_SCAN: "on"` +
`E2B_CONTROL_PLANE_URL` + 三个 schedule 变量（与另外两个栈逐字同形）。

**钉子**：`test_c3_agent_manifest.py::test_each_compose_stack_runs_exactly_one_agent_facing_the_control_plane`
把 multinode 那条分支从"没有 Redis ⇒ 不许开扫描"改成**每一栈都必须**有 `redis`、
CP 必须有 `redis://:` 的 `E2B_REDIS_URL`、面 B 必须有扫描与 CP URL —— 即"有 store 就必须开扫描"。

### 缺口 ④：compose 自愈上报的期望源地址取两个面的并集

`control_plane/c3_agent_client.py::ComposeAgentAddressResolver.resolve_host`：
期望 `source_ips` = `E2B_C3_AGENT_URL` 的 host **并** `E2B_C3_AGENT_MAINT_URL` 的 host 解析出的地址
（去重、保序）。没配面 B 的车道仍只有一条地址；k8s 车道不动（两个面共用一个 pod IP）。

**钉子**：`tests/unit/test_c3_agent_client.py`（并集含 AAAA；没配面 B 就只有面 A 一条）、
`tests/unit/test_c3_self_heal_sweep.py::test_a_compose_report_is_accepted_from_either_face`
（面 B 的地址上报 200 且孤儿真的被删、面 A 的地址也收、第三个地址仍 403 且报文里列出并集）。

### 缺口 ⑤：agent 入口放开 INFO

`deploy/c3_agent/config.py` 加 `log_level`（`E2B_LOG_LEVEL`，默认 `INFO`，与
`envd_service.config` / `control_plane.config` 同名同义）；`deploy/c3_agent/__main__.py` 加
`_configure_logging`（逐条照抄两个兄弟入口：未知级别名回落 INFO、`basicConfig` + 显式 `setLevel`）
并把 `log_level=settings.log_level.lower()` 交给 uvicorn。

**钉子**：`tests/unit/test_c3_agent_logging.py`（轮次那一行真的被 emit；默认 INFO 且 DEBUG 被丢；
`E2B_LOG_LEVEL=DEBUG` 生效；未知级别回落 INFO —— 与 `tests/unit/test_envd_worker_logging.py` 同形）。

## 3. 现场验收（1–6）

栈的起法（全部命令的 cwd = `tmp/wt-c3-compose`）：

```bash
# 本树构建（wheels/fork 是 gitignore 的构建产物，worktree 里没有 ⇒ 先从主检出拷过来）
docker buildx build --load -f deploy/docker/Dockerfile.control-plane-gateway -t e2b-sandlock-control-plane-gateway:c3-gaps .
docker buildx build --load -f deploy/docker/Dockerfile.agent                     -t e2b-sandlock-agent:c3-gaps .
docker buildx build --load -f deploy/docker/Dockerfile.envd                      -t e2b-sandlock-worker:c3-gaps .

WORKER_IMAGE=e2b-sandlock-worker:c3-gaps docker compose \
  -f deploy/compose/docker-compose.multinode.yml \
  -f tmp/c3-compose-gaps/compose.images.override.yml \
  -f tmp/c3-compose-gaps/compose.port.override.yml \
  -f tmp/c3-compose-gaps/compose.capacity.override.yml \
  -p c3gaps up -d --no-build
```

三个本地 override（都不是清单改动，留在 gitignore 的 `tmp/` 里，文件头各自写了理由）：
`compose.images.override.yml`（把每个服务指向本树的 `:c3-gaps`）、
`compose.port.override.yml`（**本机 127.0.0.1:3100 被一个上轮遗留的
`kubectl -n sandlock port-forward svc/control-plane 3100:3000` 占着**，所以本栈改发 3300，
所有命令都走 `--api http://127.0.0.1:3300`）、`compose.capacity.override.yml`（清单的
2048MB/200%/256 进程只放得下一个箱，冒烟的 4 个箱子放不下 —— 与上一轮 rig 同值）。

> ⚠ 顺手记一条环境坑：第一次用 `curl 127.0.0.1:3100` 时请求**打到了那台 k0s 集群的
> 控制面**（不是本机栈），`POST /volumes`、`POST /sandboxes` 都只拿到 `401`（key 不同），
> **没有产生任何写入**。认集群/认端口的自检在这里同样适用。

### 1. CP 跑在 65534，且没有多余的 root 容器

```console
$ docker exec c3gaps-control-plane-1 id
uid=65534(nobody) gid=65534(nogroup) groups=65534(nogroup)
$ docker exec c3gaps-control-plane-1 sh -c 'grep -E "^(Uid|CapEff|CapBnd)" /proc/self/status'
Uid:	65534	65534	65534	65534
CapEff:	0000000000000000
CapBnd:	00000000a80425fb
```

（`CapBnd` 是 runtime 默认集 —— 与 k8s 的 CP 主容器一致：那边也只钉 `runAsUser/runAsGroup`，
不写 `capabilities.drop`；非 root ⇒ `CapEff=0`。）

整栈的身份清单（`docker inspect … --format '{{.Config.User}}'`，证据 `evidence-1-uids.txt`）：

```
c3gaps-image-cache-init-1        0:0           exited 0     ← 一次性交棒，必需
c3gaps-redis-1                                running      ← image 默认（与 k8s redis 同）
c3gaps-control-plane-1           65534:65534   running      ← 本轮的改动
c3gaps-c3-agent-1                65534:65534   running
c3gaps-c3-agent-maint-1          0:0           running      ← 面 B，NFS chown 必需
c3gaps-c3-agent-proxy-1          0:0           running      ← 判据 16 的本地仪器（不在清单里）
c3gaps-worker-1/2/3-1            65534:65534   running
```

**清单里只有两个 root 服务**（`image-cache-init`、`c3-agent-maint`），proxy 是本地 override 加的
测量仪器。这条现在是清单钉子：`test_every_compose_control_plane_runs_as_the_worker_uid`
断言"root 服务恰好是那个二元集合"、"其余要么 65534、要么落在点名的 image 默认集合里"、
两个面的 uid/cap 一字不动。

### 2. 卷创建成功，`_volumes` 的属主真的是 65534

初始化那一跑（`docker logs c3gaps-image-cache-init-1`，摘录）：

```
image-cache-init: /cache/control/_images is owned by uid 65534
image-cache-init: /cache/worker/_images is owned by uid 65534
image-cache-init: /cache/control/_builds is owned by uid 65534
image-cache-init: /cache/control/_secrets is owned by uid 65534
image-cache-init: /cache/control/_snapshots is owned by uid 65534
image-cache-init: /cache/control/_templates is owned by uid 65534
image-cache-init: /cache/control/_migrate is owned by uid 65534
image-cache-init: /cache/worker/_snapshots is owned by uid 65534
image-cache-init: /cache/worker/_migrate is owned by uid 65534
image-cache-init: /cache/control is writable by uid 65534 (owner=65534 mode=755)
image-cache-init: /cache/worker is writable by uid 65534 (owner=65534 mode=755)
image-cache-init: created /cache/control/_volumes (the platform's volume store)
image-cache-init: /cache/control/_volumes -> uid 65534 mode 755 (a non-recursive hand-over; the sandbox volume data directories below it keep their pooled uids)
image-cache-init: /cache/control/_volumes/_meta does not exist -- nothing to hand over (the control plane creates it under its own uid when it first needs it)
image-cache-init: /cache/control/_volumes is owned by uid 65534 (the volume data directories below it are left alone)
```

（这是**真交棒**那一支：`_volumes` 在这张新卷上不存在 ⇒ 由 init 以 root 建出来再交给 65534。
后面每次 `up -d` 走的是 `already belongs to uid 65534 (mode 755) -- nothing to do` 那一支。）

卷创建 + 属主读数：

```console
$ curl -sS -X POST http://127.0.0.1:3300/volumes -H 'X-API-Key: local-key' \
       -H 'Content-Type: application/json' -d '{"name":"c3-gaps-store-probe"}' -w '\nHTTP %{http_code}\n'
{"volumeID":"vol_329a97c908c8426f","name":"c3-gaps-store-probe","createdAt":"2026-09-30T01:34:38.847Z","perSandboxQuotaMb":0,"token":"tok_40ba5931216b7e4941961160"}
HTTP 201
$ docker exec c3gaps-control-plane-1 sh -c 'ls -lan /var/lib/e2b-sandboxes/_volumes; ls -lan /var/lib/e2b-sandboxes/_volumes/_meta'
drwxr-xr-x 1 65534 65534  50 .            drwxr-xr-x 1 65534 65534 50 .
drwxr-xr-x 1 65534 65534  50 _meta        -rw-r--r-- 1 65534 65534 266 vol_329a97c908c8426f.json
drwxrwxrwt 1 65534 65534   0 vol_329a97c908c8426f
$ docker exec c3gaps-control-plane-1 stat -c '%n %u:%g %a' /var/lib/e2b-sandboxes{,_volumes,_volumes/_meta}
/var/lib/e2b-sandboxes 65534:65534 755
/var/lib/e2b-sandboxes/_volumes 65534:65534 755
/var/lib/e2b-sandboxes/_volumes/_meta 65534:65534 755
```

（`vol_…` 建出来是 `1777` —— D24 那条"CP 保留自己的 `mkdir` + `chmod 1777`"的行为原样。）

### 3. worker 到不了 agent；CP 到得了

```console
$ docker inspect -f '{{.Name}} {{range $k,$v := .NetworkSettings.Networks}}{{$k}}={{$v.IPAddress}} {{end}}' \
      c3gaps-c3-agent-1 c3gaps-c3-agent-maint-1 c3gaps-control-plane-1 c3gaps-worker-1-1
/c3gaps-c3-agent-1       c3gaps_agent-plane=192.168.117.2
/c3gaps-c3-agent-maint-1 c3gaps_agent-plane=192.168.117.3
/c3gaps-control-plane-1  c3gaps_agent-plane=192.168.117.4 c3gaps_default=192.168.147.6
/c3gaps-worker-1-1       c3gaps_default=192.168.147.5

$ docker exec c3gaps-worker-1-1 python3 …            # 只挂 default 的 worker
c3-agent       -> REFUSED at the name layer (gaierror: [Errno -2] Name or service not known)
c3-agent-maint -> REFUSED at the name layer (gaierror: [Errno -2] Name or service not known)

$ docker exec c3gaps-control-plane-1 python3 …       # 同时挂两张网的 CP
resolve c3-agent: 192.168.117.2 ; connect 192.168.117.2:49985: ok
resolve c3-agent-maint: 192.168.117.3 ; connect 192.168.117.3:49986: ok
```

反向那一半（k8s Egress 规则的等价物）在本机是**真的**：

```console
$ docker exec c3gaps-c3-agent-1 python3 …            # 面 A 去敲 worker
resolve worker-1: FAILED (gaierror: [Errno -3] Temporary failure in name resolution)
connect 192.168.147.5:49983: FAILED (OSError: [Errno 101] Network is unreachable)
$ docker exec c3gaps-c3-agent-1 python3 …            # 面 A 去 CP（自愈上报那条）
resolve control-plane: 192.168.117.4 ; connect 192.168.117.4:3000: ok
```

**⚠ 本机证明不了 IP 那一半（如实记账）**：OrbStack 的已知行为是"不同 user-defined 网络之间
仍可按 IP 互通"（上游 orbstack#1944 / #2492），实测 worker 直连 `192.168.117.2:49985`
**是通的**（对照组：两个临时网络 + 一个监听容器，同样直连通的）。标准 Docker daemon 会由
`DOCKER-ISOLATION-STAGE-2` 丢掉这条包；OrbStack 的 VM 里连 `iptables` 这个命令都没有
（`docker run --rm --privileged --pid=host alpine iptables -S` → `iptables: not found`）。
所以本机可证的是**两层**：

* **名字层**：worker 解析不到 `c3-agent` / `c3-agent-maint`（docker embedded DNS 按网络）；
* **凭据层**：真把 IP 打过去也没用 —— 面 A 按 token 具名拒（worker 手里根本没有 token）：

```console
$ docker exec c3gaps-worker-1-1 python3 …   # 无 token / 错 token
POST /internal/nodes/c3-agent/agent/grant-slot (no token)              -> 401 {"error":"unauthorized"}
POST /internal/nodes/c3-agent/agent/grant-slot {'X-Internal-Key':'…'}  -> 401 {"error":"unauthorized"}
$ docker exec c3gaps-control-plane-1 python3 …   # CP 用真 token（body 故意不合规）
POST /internal/nodes/c3-agent/agent/grant-slot -> 422 {"error":"the instruction body does not fit GrantSlotBody"}
```

（422 说明控制面那一发**已经过了 token 与"这条指令是发给我的吗"两道**，只卡在 body 形状；
端到端那一半由 §5 的判据 13/16 与冒烟覆盖。）

### 4. multinode 的自愈扫描是活的（门 (a) 不再推迟）

面 B 的日志（`E2B_C3_AGENT_SCAN=on`，30s 首扫 + 120s 周期）：

```
INFO:deploy.c3_agent.app:c3-agent inventory: the scan loop started (first round in 30s, then every 120s, deferral backoff capped at 600s)
INFO:deploy.c3_agent.scan:c3-agent inventory: node=c3-agent scanned=0 protected=0 orphans=0 removed=0 failed=0 deferred=-
```

`deferred=-` 就是门 (a)（"记录必须是共享 store"）不再生效的读数（控制面这一侧同时能看到
`POST /internal/nodes/c3-agent/agent/inventory … 200 OK`）。为了证明它不只是"不推迟"，
手工放一棵沙箱形状的孤儿树（`/var/lib/e2b-sandboxes/sbx_<32 hex>/`，65534）：

```console
$ docker exec -u 65534 c3gaps-c3-agent-maint-1 sh -c 'mkdir -p /var/lib/e2b-sandboxes/sbx_a6222a9ad4315b642bcfdee832a88262/workspace && …'
drwxr-xr-x 1 65534 65534 42 sbx_a6222a9ad4315b642bcfdee832a88262
# 下一轮（≤2.5 分钟）
INFO:deploy.c3_agent.scan:c3-agent inventory: node=c3-agent scanned=1 protected=0 orphans=1 removed=1 failed=0 deferred=-
$ docker exec -u 65534 c3gaps-c3-agent-maint-1 sh -c 'ls /var/lib/e2b-sandboxes'
_images  _migrate  _runtime  _snapshots          # 树没了
```

**反臂**（把面 B 的地址从期望值里拿掉，只留面 A ⇒ 就是修复前那条车道的形状）：
把 CP 的 `E2B_C3_AGENT_MAINT_URL` 置空重滚一轮，面 B 立刻：

```
WARNING:deploy.c3_agent.scan:c3-agent inventory: could not report 0 tree(s) to the control plane:
the control plane refused the inventory report (status 403): a report for agent c3-agent came from
192.168.117.3, expected 192.168.117.2 (retrying in 240s)
```

这正是缺口 ④ 的现场读数（也是"给 Redis 就活了"这个前提不成立的原因）；随之恢复原清单，
轮次回到 `deferred=-`。

### 5. 冒烟与判据 13/16

```console
$ E2B_API_URL=http://127.0.0.1:3300 E2B_SANDBOX_URL=http://127.0.0.1:3300 E2B_API_KEY=local-key \
  E2B_INTERNAL_API_KEY=internal-key .venv/bin/python deploy/scripts/multinode_smoke.py
NODE DISTRIBUTION: {'http://worker-1:49983': 2, 'http://worker-2:49983': 1, 'http://worker-3:49983': 1}
ALL sandboxes: commands + files + health through gateway OK
stdin through gateway OK
after kill reservations: [('worker-1', 0), ('worker-2', 0), ('worker-3', 0)]
MULTI-NODE SMOKE OK
```

（这一栈是 3 个 worker；4 个箱子落成 2+1+1，脚本自身的判据是"至少两个节点 + kill 后预约归 0"。）

判据 13/16 用同一栈 + `tmp/c3-compose-gaps/compose.hop-proxy.override.yml`
（记录型 CP→agent 代理，判据 16 的仪器；它同时挂 `default` 与 `agent-plane`，因为 CP 在默认网上
拨它、它要转到 `agent-plane` 上的真 agent）。两条驱动器的默认 `--override` 是
`tmp/acc-13-16/compose.override.yml` 且 `action="append"`（argparse 会把默认值一起带上），
所以在 `tmp/acc-13-16/compose.override.yml` 放了一个 `services: {}` 的空壳（文件头写了理由）。

```console
$ .venv/bin/python deploy/scripts/acceptance/c3_accept_13_reverse_lookup.py \
    --compose deploy/compose/docker-compose.multinode.yml \
    --override tmp/c3-compose-gaps/compose.images.override.yml \
    --override tmp/c3-compose-gaps/compose.port.override.yml \
    --override tmp/c3-compose-gaps/compose.capacity.override.yml \
    --override tmp/c3-compose-gaps/compose.hop-proxy.override.yml \
    --project c3gaps --api http://127.0.0.1:3300 --logdir tmp/c3-compose-gaps/logs \
    --worker-image e2b-sandlock-worker:c3-gaps --agent-image e2b-sandlock-agent:c3-gaps --shared-pid 300
  PASS right-A resolved a host pid -- 3677428
  PASS right-B resolved a host pid -- 3677499
  PASS the two workers' children have different HOST pids -- 3677428 vs 3677499
  PASS counter-arm 1 refused with the named message, verbatim -- "the control plane refused the slot-identity report for sandbox sbx_95acc00365444cbb (HTTP 502): the agent for node worker-3 refused the grant: container pid 300 is not in worker worker-3's pid namespace (pid:[4026533710]): refusing"
  PASS counter-arm 2 refused with the named message, verbatim -- "… container pid 317 is not in worker worker-1's pid namespace (pid:[4026533268]): refusing"
JUDGMENT 13: all assertions passed

$ .venv/bin/python deploy/scripts/acceptance/c3_accept_16_concurrent_slots.py … --rounds 5 --baseline-rounds 3 --counter-rounds 3 --hop-delay 0.25
   round 1..5: grants=3 grants_max_in_flight=3 total_seconds 2.398/2.489/2.547/2.389/2.404
   baseline   : grants=3 grants_max_in_flight=1 total_seconds 5.709/4.462/4.148
  PASS every concurrent round succeeded (all creates + all slot starts)
  PASS no superlinear slowdown vs the serial baseline -- concurrent 2.547s vs serial 5.709s
  PASS the hop really ran concurrently (more than one grant in flight) -- max in flight 3
  PASS the counter-arm reproduced the queueing (one grant at a time) -- max in flight [1, 1, 1]
JUDGMENT 16: all assertions passed
```

### 6. 覆盖所改清单的 unit/contract 测试

```console
$ .venv/bin/python -m pytest $(ls tests/unit | grep -E "c3|compose|worker|k8s|deploy|envd_worker_logging" ...) \
      tests/unit/test_docs_only_point_at_repo_artifacts.py -q
423 passed in 34.48s

$ .venv/bin/python -m pytest tests/contract/test_c3_worker_kernel_identity.py tests/contract/test_c3_slot_identity_grant.py -q
8 passed, 11 skipped in 12.81s
# 11 skipped 全是 macOS 上跑不了的真内核用例（Linux-only 判据），与 main 同态

$ .venv/bin/python -m pytest tests/unit --collect-only -q
SKIPPED … 'fakeredis': No module named 'fakeredis'（7 个文件）
ERROR tests/unit/test_pause_quota.py    # ModuleNotFoundError: No module named 'redis'
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
# 预存在：在 main 的干净检出上单独收同一个文件，报的是同一个错
```

## 4. 钉子一览（防回退）

| 性质 | 钉子 |
|---|---|
| 三个 compose CP 都是 `65534:65534`，且 root 只有 `image-cache-init` / `c3-agent-maint` | `tests/unit/test_c3_agent_manifest.py::test_every_compose_control_plane_runs_as_the_worker_uid` |
| agent 的两个面只在 `agent-plane`、worker 一个都不在、CP 同时在两张网 | 同文件 `test_the_compose_agent_channel_is_a_network_no_worker_joins` |
| 每一栈都有 Redis、CP 有 `redis://` 的 `E2B_REDIS_URL`、面 B 开着扫描 | 同文件 `test_each_compose_stack_runs_exactly_one_agent_facing_the_control_plane` |
| 卷存储交棒：非递归、幂等、逐目标校验、失败具名（三个栈的正文都真跑） | `tests/unit/test_c3_cp_rootless.py` 新增一节（文本 + 3 臂 × 3 栈） |
| 交棒目标 = CP 真正写的那条路径 | 同文件 `test_each_compose_init_hands_the_volume_store_over_like_storage_init` |
| compose 上报的期望地址 = 两个面的并集；第三个地址仍拒 | `tests/unit/test_c3_agent_client.py`、`tests/unit/test_c3_self_heal_sweep.py::test_a_compose_report_is_accepted_from_either_face` |
| agent 的 INFO（自愈轮次那一行）可见 | `tests/unit/test_c3_agent_logging.py` |

## 5. 没能收口 / 需要知道的事

1. **连接层的 IP 那一半在本机不成立（daemon 实现问题，不是清单问题）**：OrbStack 不实现
   Docker 的跨网隔离（上游 orbstack#1944 / #2492；其 VM 里没有 `iptables`），所以本机从
   worker 直连 agent 的 agent-plane 地址是通的。清单本身给出的是标准 Docker 的性质
   （专用网络 + `DOCKER-ISOLATION-STAGE-2`），本机可证的只有"名字层 + 凭据层"。
   **要在 IP 层也拿到实测，需要一台实现了该隔离的 Docker daemon**（目标机的原生 Docker 属于
   这一类）；本机验收按两层读，别把它当成"IP 也被挡了"的证据。
2. **`agent-plane` 上多一个判据 16 的仪器**：`c3-agent-proxy`（本地 override 加的，不在清单里）
   为了转发必须同时在两张网上。跑完 13/16 后我把栈留着没拆，若要复现请注意这一点。
3. **本机 127.0.0.1:3100 仍被一个上轮遗留的 `kubectl port-forward` 占着**（PID 69435，
   2026-09-29 22:40 起）。我**没有动它**（它指向 k0s 集群），本栈因此发 3300。
   任何"curl 127.0.0.1:3100"在这台机上都会打到那套集群 —— 提醒见 §3 的开头。
4. **三个 compose 栈只在 multinode 上做了真机验收**：`deploy/compose/docker-compose.prod.yml`
   与 `deploy/stack/docker-compose.prod.yml` 的改动与其**同一份脚本正文**（只换挂载路径），
   由 `test_c3_cp_rootless.py` 的 3 臂 × 3 栈行为钉子覆盖；没有为它们各起一套栈
   （stack 还会拉 buildkit/quota-agent，代价与本轮目标不成比例）。
5. **缓存目录那条 `chown -R` 我顺手改成了 k8s 的谨慎形**（只有 `_oci/` 递归、`secrets/` 只动目录）：
   原来的 `-R` 会在每次 `up -d` 把 `secrets/<sandbox_id>/<name>.secret`（活沙箱的 0600 文件）
   夺回 65534。这条不在三个缺口的字面里，但同一段代码、同一类"夺回别人东西"的错，且 k8s 侧
   `storage-init` 的注释把递归形点名为"不许再获得的回归"；`upgrade.sh` 的一次性 `chown -R`
   仍在（迁移路径不变）。
6. **`deploy/stack/.version` 与镜像 push 都没做**：本轮没有上线，也没有 push 任何镜像；
  三个 `:c3-gaps` tag 只在本机。

## 6. 评审轮（2026-09-30，同一 worktree/分支）

评审的结论：三条缺口与两个额外修复都落地、现场证据成立；问题出在**我上一轮没起过的
`deploy/stack`**（两条回归），外加一条陈旧的文档现在时、一条缺失的钉子与若干注释/测试卫生。
下面是逐条的收口与**两条新的现场读数**。原始输出：
`tmp/c3-compose-gaps/evidence-1b-buildkit-*.txt`、`evidence-2-tls-*.txt`、
`evidence-6-init-caps.txt`、`evidence-6-caps-arms.txt`、`stack-up.log`。

### 6.1 提交

| commit | 内容 |
|---|---|
| `200e7bf` | `fix(compose): CP 读得到 buildkit socket、init 收到 storage-init 的能力集` —— 第 1、5、6 条 + 钉子（含第 4、8 条） |
| `949575d` | `fix(scripts): TLS 配方产出 65534 的控制面读得到的证书对` —— 第 2 条 + 单元/契约两条钉子 |
| `5849398` | `docs(c3): CP 65534 带出的两条回归与收口记进账本` —— 第 3、7、9 条 |
| `<本报告>` | `chore(c3): 追加评审轮的报告` |

### 6.2 两条新回归的现场读数

两个读数都取自 **`deploy/stack` 形态**（`-p c3stack`，镜像同样从本树构建的 `:c3-gaps`）——
那正是有 buildkit、也挂了 `./tls` 的栈，也就是我上一轮没起的那一个。起法（新增一个本地
override，只指镜像 + 把端口挪到 3400，避开 3100 上那个遗留的 `kubectl port-forward`）：

```bash
docker compose -f deploy/stack/docker-compose.prod.yml \
  -f tmp/c3-compose-gaps/compose.stack.override.yml -p c3stack up -d --no-build
```

**① buildkit 的 unix socket（第 1 条）**

```console
$ docker exec c3stack-control-plane-1 sh -c 'id; stat -c "%n %F %a %u:%g" /run/buildkit/buildkitd.sock'
uid=65534(nobody) gid=65534(nogroup) groups=65534(nogroup),1000
/run/buildkit/buildkitd.sock socket 660 1000:1000
$ docker exec c3stack-control-plane-1 sh -c 'grep ^Groups /proc/self/status'
Groups:	1000 65534
$ docker exec c3stack-control-plane-1 buildctl --addr unix:///run/buildkit/buildkitd.sock debug workers
ID                            PLATFORMS
qaaf72gm7s2a34gq2xdk2lb0u     linux/amd64,linux/amd64/v2,linux/amd64/v3,linux/arm64,…   # rc=0
$ docker run --rm -u 65534:65534 -v c3stack_buildkit-data:/run/buildkit:ro \
    --entrypoint buildctl e2b-sandlock-control-plane-gateway:c3-gaps \
    --addr unix:///run/buildkit/buildkitd.sock debug workers
error: failed to list workers: Unavailable: connection error: desc = "transport: Error while
dialing: dial unix /run/buildkit/buildkitd.sock: connect: permission denied"     # ← 反臂：上一轮发出的形态
$ docker run --rm -u 65534:65534 --group-add 1000 -v c3stack_buildkit-data:/run/buildkit:ro \
    --entrypoint buildctl e2b-sandlock-control-plane-gateway:c3-gaps \
    --addr unix:///run/buildkit/buildkitd.sock debug workers | head -2
ID                            PLATFORMS                                                  # ← 加了组就通
```

（`docker exec -u 65534:65534` **当不了反臂**：exec 会带上容器的 `GroupAdd`，
`Groups:` 仍是 `1000 65534`、照样连得上 —— 反臂必须用 `docker run -u` 且**不加**组重建那个
形态。这一条同时写进了 `docs/c3-privilege-relocation.md` §13.6.1。）

**② TLS 配方（第 2 条）**

先在卷里摆出**原生 Linux 语义**的属主关系（`root:root 0600`）。之所以用命名卷而不是宿主
bind mount：OrbStack 会把宿主 bind mount 呈现成"容器自己的 uid"（root 容器看到 `0:0`、65534
容器看到 `65534:65534`，实测），**在 macOS 上根本复现不出**"不是自己的 0600"这个形状。

```console
$ docker run --rm -u 65534:65534 -v c3stack_tlsdata:/tls:ro alpine \
    sh -c 'stat -c "%n %a %u:%g" /tls/tls.key; head -c1 /tls/tls.key >/dev/null && echo readable || echo "NOT readable"'
/tls/tls.key 600 0:0
NOT readable by 65534
$ # 旧配方（0600）+ TLS 打开
$ docker inspect c3stack-control-plane-1 --format 'status={{.State.Status}} exit={{.State.ExitCode}} restarts={{.RestartCount}}'
status=restarting exit=1 restarts=6
$ docker logs c3stack-control-plane-1 | tail -3
  File "/usr/local/lib/python3.14/site-packages/uvicorn/config.py", line 129, in create_ssl_context
    ctx.load_cert_chain(certfile, keyfile, get_password)
PermissionError: [Errno 13] Permission denied
$ docker run --rm -v c3stack_tlsdata:/tls alpine chmod 644 /tls/tls.key      # 修好之后的模式
$ docker inspect c3stack-control-plane-1 --format 'status={{.State.Status}}'
status=running
$ docker logs c3stack-control-plane-1 | grep -i "Uvicorn running" | tail -1
INFO:     Uvicorn running on https://0.0.0.0:3000 (Press CTRL+C to quit)
$ curl -sS -k https://127.0.0.1:3400/healthz -w ' HTTP %{http_code}\n'
{"status":"ok"} HTTP 200
$ curl -sS http://127.0.0.1:3400/healthz -w ' HTTP %{http_code}\n'             # 同端口明文必须失败
HTTP 000
$ echo | openssl s_client -connect 127.0.0.1:3400 -servername control-plane 2>/dev/null \
    | openssl x509 -noout -subject -ext subjectAltName
subject=CN=localhost
X509v3 Subject Alternative Name: DNS:localhost, IP Address:127.0.0.1,
    IP Address:0:0:0:0:0:0:0:1, DNS:control-plane
```

（跑完这两条现场后我把该栈的 CP 恢复成 HTTP 并确认 `{"status":"ok"}`，
worker 重新注册；multinode 栈也按下面 §6.4 重跑了一遍。）

### 6.3 能力集（第 6 条）的现场读数

`image-cache-init` 现在与 k8s `storage-init` 逐条同形（`drop: [ALL]` + 三条）；
用容器真跑那份脚本正文（`tmp/c3-compose-gaps/init-cache.sh`，取自清单本体）：

```console
$ docker inspect c3gaps-image-cache-init-1 --format 'user={{.Config.User}} cap_drop={{.HostConfig.CapDrop}} cap_add={{.HostConfig.CapAdd}}'
user=0:0 cap_drop=[ALL] cap_add=[CAP_CHOWN CAP_DAC_OVERRIDE CAP_FOWNER]
$ # 臂 1：清单那三条
$ docker run --rm -u 0:0 --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER \
    -v c3caps_probe:/w -v "$PWD/tmp/c3-compose-gaps:/src:ro" \
    -e CACHE_DIRS=/w/_images -e OWNED_DIRS=/w/_secrets -e WRITABLE_ROOTS=/w -e VOLUME_STORES=/w/_volumes \
    --entrypoint sh e2b-sandlock-worker:c3-gaps -c 'grep -E "^Cap(Bnd|Eff)" /proc/self/status; sh /src/init-cache.sh'
CapEff:	000000000000000b        # CHOWN|DAC_OVERRIDE|FOWNER
CapBnd:	000000000000000b
image-cache-init: /w/_images is owned by uid 65534
… image-cache-init: /w/_volumes -> uid 65534 mode 755 (a non-recursive hand-over; …)
$ # 臂 2（反臂）：只给 CHOWN+FOWNER
CapEff:	0000000000000009
mkdir: cannot create directory '/w/_images/_oci': Permission denied      # ← 三条里 DAC_OVERRIDE 的作用
```

### 6.4 第 1、4、5、6、7、8、9 条的收口与复跑

* **第 1 条**：`deploy/stack` 的 `control-plane` 加 `group_add: ["1000"]`；钉子
  `test_only_the_stack_lane_joins_the_builders_group` 两个方向都钉（该栈必须有；
  另外两个栈没有 builder，**不得**有）。§6.2① 是现场读数。
* **第 2 条**：生成器两个文件都 `0644` + 脚本注释写清"为什么是模式而不是属主/组"；
  钉子 = 单元（真跑生成器 + 断言两个生产栈的挂载/环境变量，multinode 形态钉成"没有"）
  + 契约（真容器 `load_cert_chain` 两条臂）。§6.2② 是现场读数。
* **第 3 条**：`docs/production-deployment-requirements.md` 的现在时改写 + 那张 2026-09-13
  的表/验收项标注为 root 时代的历史；顺带 `docs/task-backlog.md`、`docs/sandbox-disk-quota.md`
  两处同源句子。§7.11/§11.2.1 已按本轮补齐（§11.2.1 第 16 条、§7.12）。
* **第 4 条**：缓存交棒的**非递归**变成精确钉子 —— 单元里缓存那一半是**有序行列表**相等
  （`CACHE_HANDOVER_LINES`），另加 `chown -R 65534:65534 "$dir"` 的禁项；改回 `-R` 现在
  必红（评审前：一条都不会红）。
* **第 5 条**：三个栈的注释改成"**agent DaemonSet 的** init；控制面 pod 自 C3 Task 5 起
  **故意没有** init"；multinode 那句过宽的注释改成"可写根**之下**的 `state`/`_runtime`/
  `.route-b` 由 CP 以根属主身份自己建，所以可写根也在交棒清单里"。
* **第 6 条**：见 §6.3；钉子 `test_each_compose_init_drops_to_the_three_verbs_the_script_uses`
  直接与 k8s `storage-init` 的能力集比较相等。
* **第 7 条**：`Dockerfile.control-plane-gateway` 那句"the control plane itself runs as root"
  改成"CP **就是**那个 65534（k8s `runAsUser` / 三个 compose 栈的 `user:`），所以这行不再只是
  为 worker 铺路"。
* **第 8 条**：`DECLARED_USERS` 从子集判断改成**精确字典**；redis 的
  `E2B_REDIS_URL` 口令与服务的 `--requirepass`、healthcheck 的 `-a` 逐字绑定（新测试
  `test_every_compose_record_store_is_wired_with_one_password`）；worker 那半补成
  `declared == ["default"]`（原来只钉"不在 agent-plane"）；行为臂的三条 stdout 全部改成
  **精确列表**（原来是 `in`/`startswith`）；另加"三个栈的 init 命令行逐字相同"。
* **第 9 条**：§7.11 的 417 → **423**（与 §3.6 同一次运行）。
* **复跑**（第 1/5/6 条改到的 multinode 清单会进冒烟，所以照规矩全跑）：相关 unit
  **454 passed**；contract `10 passed, 11 skipped`（跳过的是 macOS 跑不了的真内核用例，
  与 main 同态）；`multinode_smoke.py` = **`MULTI-NODE SMOKE OK`**（4 箱 2+1+1）；
  判据 13 **all assertions passed**、判据 16 **all assertions passed**（并发 in-flight 3、
  反臂 1）。三个 `docker compose config` 都 ok。

### 6.5 这一轮剩下的顾虑

1. **`deploy/compose/docker-compose.prod.yml` 仍只有静态/钉子覆盖**：它没有 buildkit（也就没有
   组位问题），init 的改动与 stack/multinode 是**同一份命令行**（已钉"三栈逐字相同"），
   TLS 那条则与 stack 同形（同一个 `./tls:/tls:ro` + 同样的 env）。要真机验收它，需要一个
   registry profile 与 3 个 worker 的完整栈 —— 与本轮目标不成比例，我没有起。**但这一轮
   的教训正面写了**：上一轮的 `deploy/stack` 就是"没起过"才漏掉的，所以这一轮**起了**
   `deploy/stack`（§6.2 两条读数都来自它）。
2. **TLS 打开之后 worker 还要信任自签 CA**（并把 `E2B_CONTROL_PLANE_URL` 指到 https）：这条
   配方注释里早就写着，本轮只保证**控制面自己起得来**，没有替运维做那一步。
3. **`group_add: ["1000"]` 是一个真实的组位**：CP 因此能读宿主/卷里"组 1000 可读"的文件。
   它与 k8s 的 `fsGroup: 1000` 是同一个组、同一个理由（buildkit socket），不是新开口子；
   但它是"CP 零特权"里唯一一处靠**组**而不是靠**属主**的权限，值得在下一次身份评审时一起看。
