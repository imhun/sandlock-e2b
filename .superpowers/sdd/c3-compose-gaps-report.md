# C3 compose 车道：三条已知缺口的收口（含做验收时量到的两个真缺陷）

- 工作目录 `/Users/polus/project/ai/sandlock-e2b`，worktree `tmp/wt-c3-compose`，
  分支 **`feat/c3-compose-gaps`**（从 `main` 的 C3 合并点 `a166548` 起）
- 未 push、未 merge、未碰 k0s 集群；所有现场读数来自**本机 multinode 栈**（OrbStack，
  linux/amd64），镜像全部从**本树**构建（`:c3-gaps`）
- 原始日志/读数：`tmp/wt-c3-compose/tmp/c3-compose-gaps/`（`evidence-*.txt`、`*.log`、`logs/`）

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
